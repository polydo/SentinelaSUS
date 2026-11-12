"""
SentinelaSUS - Sobrepreço em compras públicas de medicamentos (Brasil)
Fonte: PostgreSQL local (dbSentinelaSUS) -> fato_bps x dim_pmvg x dim_calendario

Como rodar:
    python -m pip install streamlit pandas plotly requests sqlalchemy "psycopg[binary]"
    python -m streamlit run app_brasil.py
"""
import json
import os
import re
import unicodedata
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from sqlalchemy import bindparam, create_engine, text
from sqlalchemy.engine import URL


# ----------------------------------------------------------------------------
# CONFIGURAÇÃO
# ----------------------------------------------------------------------------
PG_HOST = "aws-1-us-east-1.pooler.supabase.com"
PG_PORT = 5432
PG_DB = "postgres"
PG_USER_PADRAO = "postgres.fdgrtuwhyccshnugcefy"
PG_SENHA_PADRAO = "Amp07062020@@**"
PG_SCHEMA_PADRAO = "public"

T_FATO, T_PMVG, T_CAL, T_ICMS = "fato_bps", "dim_pmvg", "dim_calendario", "dim_icms"
TOLERANCIA_PADRAO = 2.0  # % acima do preço PMVG para considerar suspeito
GEO_CACHE = Path(__file__).with_name("geo_br_ufs_cache.json")

# Nomes prováveis de colunas (a primeira que existir é usada; tudo pode ser trocado na barra lateral)
CAND = {
    "f_registro": ["registro", "registro_anvisa", "nu_registro", "cd_registro"],
    "f_preco": ["vl_preco_unitario", "preco_unitario", "vl_unitario"],
    "f_municipio": ["no_municipio", "nome_municipio", "municipio", "ds_municipio"],
    "f_uf": ["sg_uf", "uf", "sigla_uf", "no_uf", "uf_compra", "sg_estado", "estado"],
    "f_qtd": ["qt_medicamento", "qtd_itens_comprados", "qt_itens_comprados", "qtd_comprada", "quantidade", "qtd"],
    "f_data": ["dt_compra", "data_compra", "data"],
    "f_id": ["nu_processo_compra", "id_compra", "nu_compra"],
    "f_inst": ["no_instituicao", "nome_instituicao", "instituicao"],
    "f_forn": ["no_fornecedor", "nome_fornecedor", "fornecedor"],
    "p_subst": ["SUBSTÂNCIA", "substancia", "no_substancia"],
    "p_registro": ["registro", "registro_anvisa", "nu_registro"],
    "p_qtd": ["QUANTIDADE", "qt_apresentacao", "qtd_apresentacao"],
    "i_estado": ["estado", "sg_uf", "uf"],
    "i_icms": ["icms", "pc_icms", "aliquota_icms", "aliquota"],
    "c_data": ["data", "dt_data", "date", "dt_calendario"],
}
TIPOS_DATA = {"date", "timestamp without time zone", "timestamp with time zone"}
NENHUMA = "(nenhuma)"

st.set_page_config(page_title="SentinelaSUS", page_icon="🛡️", layout="wide")

st.markdown(
    """
    <style>
    .stApp { background-color: #0f1523; color: #e8eaf0; }
    [data-testid="stSidebar"] { background-color: #151c2c; }
    header[data-testid="stHeader"] { background: transparent; }
    .block-container { padding-top: 1.5rem; max-width: 1600px; }
    h1.titulo { font-size: 2.4rem; font-weight: 700; margin: 0 0 1rem 0; color: #fff; }
    .card { background: #1c2333; border-radius: 10px; padding: 16px 22px; min-height: 150px; }
    .card .rotulo { font-size: 1rem; color: #dfe3ee; margin-bottom: 10px; min-height: 2.6em; }
    .card .valor { font-size: clamp(1.6rem, 2.4vw, 2.6rem); font-weight: 700; color: #fff;
                   line-height: 1.1; white-space: nowrap; }
    .card .icone { color: #e03131; font-size: 0.85em; margin-right: 10px; }
    .painel-titulo { font-size: 1.15rem; color: #dfe3ee; margin: 8px 0 0 4px; }
    .tag-vermelha { display: inline-block; background: #d62f2f; color: #fff; font-size: 1.15rem;
                    padding: 2px 10px; border-radius: 4px; }
    label, .stMarkdown p { color: #dfe3ee; }
    [data-testid="stWidgetLabel"] p { color: #dfe3ee !important; font-size: 1rem; }
    .stButton button, .stDownloadButton button, button[data-testid^="stBaseButton"] {
        background-color: #1c2333 !important; border: 1px solid #4a5268 !important; color: #e8eaf0 !important;
    }
    .stButton button p, .stDownloadButton button p, button[data-testid^="stBaseButton"] p {
        color: #e8eaf0 !important; font-weight: 600;
    }
    .stButton button:hover, .stDownloadButton button:hover, button[data-testid^="stBaseButton"]:hover {
        background-color: #d62f2f !important; border-color: #d62f2f !important; color: #ffffff !important;
    }
    .stButton button:hover p, .stDownloadButton button:hover p { color: #ffffff !important; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ----------------------------------------------------------------------------
# UTILITÁRIOS
# ----------------------------------------------------------------------------
def norm(texto) -> str:
    t = unicodedata.normalize("NFKD", str(texto)).encode("ascii", "ignore").decode()
    return " ".join(t.lower().split())


def chave(s) -> str:
    return re.sub(r"[\s_]+", "", norm(s))


def detectar(colunas, candidatos):
    por_chave = {chave(c): c for c in colunas}
    for cand in candidatos:
        if chave(cand) in por_chave:
            return por_chave[chave(cand)]
    return None


def q(ident: str) -> str:
    """Identificador SQL entre aspas (os nomes vêm do information_schema)."""
    return '"' + str(ident).replace('"', '""') + '"'


def sql_num(expr: str) -> str:
    """Converte para numeric aceitando vírgula decimal; texto inválido vira NULL."""
    limpo = f"replace(CAST({expr} AS text), ',', '.')"
    return f"(CASE WHEN {limpo} ~ '^-?[0-9]+(\\.[0-9]+)?$' THEN CAST({limpo} AS numeric) END)"


def sql_reg(expr: str) -> str:
    return f"ltrim(regexp_replace(CAST({expr} AS text), '\\D', '', 'g'), '0')"


def brl_curto(v: float) -> str:
    for lim, suf in ((1e9, "Bi"), (1e6, "Mi"), (1e3, "mil")):
        if abs(v) >= lim:
            return f"R$ {v / lim:,.1f} {suf}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def brl_barra(v: float) -> str:
    for lim, suf in ((1e9, "Bi"), (1e6, "Mi"), (1e3, "mil")):
        if abs(v) >= lim:
            return f"R$ {v / lim:,.1f} {suf}".replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def brl_completo(v: float) -> str:
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_int(v: float) -> str:
    return f"{int(v):,}".replace(",", ".")


def nome_municipio(s: pd.Series) -> pd.Series:
    return (
        s.astype(str).str.strip().str.title()
        .str.replace(r"\b(Da|De|Do|Das|Dos|E)\b", lambda m: m.group(1).lower(), regex=True)
    )


# ----------------------------------------------------------------------------
# GEO (estados do Brasil - IBGE)
# ----------------------------------------------------------------------------
def _centroide(geom):
    """Centroide do maior polígono (fórmula do polígono, em graus)."""
    polys = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    melhor, melhor_area = None, -1.0
    for poly in polys:
        anel = poly[0]
        a = cx = cy = 0.0
        for (x0, y0), (x1, y1) in zip(anel, anel[1:] + anel[:1]):
            cruz = x0 * y1 - x1 * y0
            a += cruz
            cx += (x0 + x1) * cruz
            cy += (y0 + y1) * cruz
        if a == 0:
            continue
        if abs(a) > melhor_area:
            melhor_area = abs(a)
            melhor = (cx / (3 * a), cy / (3 * a))
    return melhor or (-52.0, -14.0)


@st.cache_data(show_spinner="Carregando mapa do Brasil (IBGE)...")
def carregar_geo():
    if GEO_CACHE.exists():
        return json.loads(GEO_CACHE.read_text(encoding="utf-8"))
    base = "https://servicodados.ibge.gov.br/api"
    malha = requests.get(
        f"{base}/v3/malhas/paises/BR",
        params={"intrarregiao": "UF", "formato": "application/vnd.geo+json", "qualidade": "minima"},
        timeout=60,
    ).json()
    estados = requests.get(f"{base}/v1/localidades/estados", timeout=30).json()
    info = {str(e["id"]): (e["sigla"], e["nome"]) for e in estados}
    ufs = []
    for f in malha["features"]:
        cod = str(f["properties"]["codarea"])
        lon, lat = _centroide(f["geometry"])
        sigla, nome = info.get(cod, (cod, cod))
        ufs.append({"codarea": cod, "sigla": sigla, "nome": nome, "lon": lon, "lat": lat})
    out = {"geojson": malha, "ufs": ufs}
    GEO_CACHE.write_text(json.dumps(out), encoding="utf-8")
    return out


# ----------------------------------------------------------------------------
# BANCO DE DADOS
# ----------------------------------------------------------------------------
@st.cache_resource
def obter_engine(usuario, senha, host, porta, banco):
    url = URL.create("postgresql+psycopg", username=usuario, password=senha, host=host, port=porta, database=banco)
    return create_engine(url, pool_pre_ping=True)


@st.cache_data(show_spinner="Lendo estrutura do banco...", ttl=600)
def ler_estrutura(_engine, conn_id, schema):
    sql = text(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = :s AND table_name IN (:a, :b, :c, :d) ORDER BY table_name, ordinal_position"
    )
    with _engine.connect() as conn:
        return pd.read_sql(sql, conn, params={"s": schema, "a": T_FATO, "b": T_PMVG, "c": T_CAL, "d": T_ICMS})


def partes_sql(m, schema):
    F, P, C = (f"{q(schema)}.{q(t)}" for t in (T_FATO, T_PMVG, T_CAL))
    if m["cal_key"]:
        if m.get("juntar_como_data"):
            cond = f"CAST(f.{q(m['fk'])} AS date) = CAST(c.{q(m['cal_key'])} AS date)"
        else:
            cond = f"f.{q(m['fk'])} = c.{q(m['cal_key'])}"
        join_cal = f"JOIN {C} c ON {cond}"
        dt = f"CAST(c.{q(m['dt'])} AS date)"
    else:
        join_cal = ""
        dt = f"CAST(f.{q(m['dt'])} AS date)"
    return F, P, join_cal, dt


def sql_referencia(m, schema):
    """CTEs: pm (dim_pmvg linha a linha), ref (preço máximo unitário por registro e alíquota) e icms (por estado).
    Preço máximo = coluna 'PMVG <alíquota> %' da dim_pmvg dividida por QUANTIDADE."""
    P = f"{q(schema)}.{q(T_PMVG)}"
    I = f"{q(schema)}.{q(T_ICMS)}"
    cols = m["pmvg_cols"]
    colunas_sql = ", ".join(f"{sql_num('p.' + q(c))} AS p{i}" for i, (c, _) in enumerate(cols))
    pm = (
        f"pm AS (SELECT {sql_reg('p.' + q(m['p_reg']))} AS reg, CAST(p.{q(m['subst'])} AS text) AS substancia, "
        f"{sql_num('p.' + q(m['p_qtd']))} AS qtd_emb, {colunas_sql} FROM {P} p)"
    )
    uniao = " UNION ALL ".join(
        f"SELECT reg, CAST({pct} AS numeric) AS icms_pct, MAX(p{i} / NULLIF(qtd_emb, 0)) AS preco_cmed, "
        f"MAX(substancia) AS substancia FROM pm WHERE p{i} IS NOT NULL GROUP BY reg"
        for i, (_, pct) in enumerate(cols)
    )
    icms = (
        f"icms AS (SELECT upper(trim(CAST(i.{q(m['i_estado'])} AS text))) AS estado, "
        f"MAX({sql_num('i.' + q(m['i_icms']))}) AS icms_pct FROM {I} i GROUP BY 1)"
    )
    return f"{pm}, ref AS ({uniao}), {icms}"


@st.cache_data(show_spinner="Lendo períodos e localidades...", ttl=600)
def ler_metadados(_engine, conn_id, schema, m):
    F, P, join_cal, dt = partes_sql(m, schema)
    with _engine.connect() as conn:
        datas = pd.read_sql(text(f"SELECT MIN({dt}) AS dmin, MAX({dt}) AS dmax FROM {F} f {join_cal}"), conn)
        locais = pd.read_sql(
            text(
                f"SELECT DISTINCT f.{q(m['uf'])} AS uf, f.{q(m['mun'])} AS municipio "
                f"FROM {F} f WHERE f.{q(m['mun'])} IS NOT NULL"
            ),
            conn,
        )
    return datas.iloc[0]["dmin"], datas.iloc[0]["dmax"], locais


@st.cache_data(show_spinner="Cruzando fato_bps com dim_pmvg no banco...", ttl=600)
def consultar(_engine, conn_id, schema, m, d1, d2, tol, minv):
    F, P, join_cal, dt = partes_sql(m, schema)
    qtd = f"COALESCE({sql_num('f.' + q(m['qtd']))}, 1)" if m["qtd"] else "1"
    sql = f"""
    WITH {sql_referencia(m, schema)}, base AS (
        SELECT f.{q(m['uf'])} AS uf, f.{q(m['mun'])} AS municipio,
               {sql_num('f.' + q(m['preco']))} AS preco_bps, {qtd} AS qtd, p.preco_cmed
        FROM {F} f
        JOIN icms ic ON upper(trim(CAST(f.{q(m['uf'])} AS text))) = ic.estado
        JOIN ref p ON {sql_reg('f.' + q(m['reg']))} = p.reg AND p.icms_pct = ic.icms_pct
        {join_cal}
        WHERE {sql_num('f.' + q(m['preco']))} IS NOT NULL AND {dt} BETWEEN :d1 AND :d2
    ), calc AS (
        SELECT uf, municipio,
               preco_bps > preco_cmed * (1 + CAST(:tol AS numeric) / 100.0) AS suspeita,
               GREATEST(preco_bps - preco_cmed, 0) * qtd AS sobre
        FROM base
    )
    SELECT uf, municipio,
           COUNT(*) AS n_total,
           COUNT(*) FILTER (WHERE suspeita AND sobre >= CAST(:minv AS numeric)) AS n_susp,
           COALESCE(SUM(sobre) FILTER (WHERE suspeita AND sobre >= CAST(:minv AS numeric)), 0) AS sobrepreco
    FROM calc
    GROUP BY uf, municipio
    """
    with _engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params={"d1": d1, "d2": d2, "tol": float(tol), "minv": float(minv)})
    for c in ("n_total", "n_susp", "sobrepreco"):
        df[c] = pd.to_numeric(df[c]).astype(float)
    return df


@st.cache_data(show_spinner="Buscando as compras com sobrepreço...", ttl=600)
def consultar_detalhe(_engine, conn_id, schema, m, d1, d2, tol, minv, ufs_raw, pares, limite):
    F, P, join_cal, dt = partes_sql(m, schema)
    qtd = f"COALESCE({sql_num('f.' + q(m['qtd']))}, 1)" if m["qtd"] else "1"
    params = {"d1": d1, "d2": d2, "tol": float(tol), "minv": float(minv), "lim": int(limite)}
    filtros, binds = [], []
    if ufs_raw:
        filtros.append(f"f.{q(m['uf'])} IN :ufs")
        params["ufs"] = list(ufs_raw)
        binds.append(bindparam("ufs", expanding=True))
    if pares:
        conds = []
        for i, (u, mn) in enumerate(pares):
            conds.append(
                f"(f.{q(m['uf'])} IS NOT DISTINCT FROM :u{i} AND f.{q(m['mun'])} IS NOT DISTINCT FROM :m{i})"
            )
            params[f"u{i}"], params[f"m{i}"] = u, mn
        filtros.append("(" + " OR ".join(conds) + ")")
    extra = (" AND " + " AND ".join(filtros)) if filtros else ""
    sql = f"""
    WITH {sql_referencia(m, schema)}, base AS (
        SELECT CAST(f.{q(m['id'])} AS text) AS id_compra, CAST(f.{q(m['inst'])} AS text) AS ente,
               CAST(f.{q(m['forn'])} AS text) AS fornecedor, f.{q(m['uf'])} AS uf, f.{q(m['mun'])} AS municipio,
               {dt} AS data_compra, {sql_num('f.' + q(m['preco']))} AS preco_bps, {qtd} AS qtd,
               p.preco_cmed, p.substancia
        FROM {F} f
        JOIN icms ic ON upper(trim(CAST(f.{q(m['uf'])} AS text))) = ic.estado
        JOIN ref p ON {sql_reg('f.' + q(m['reg']))} = p.reg AND p.icms_pct = ic.icms_pct
        {join_cal}
        WHERE {sql_num('f.' + q(m['preco']))} IS NOT NULL AND {dt} BETWEEN :d1 AND :d2 {extra}
    ), calc AS (
        SELECT *, GREATEST(preco_bps - preco_cmed, 0) AS delta, GREATEST(preco_bps - preco_cmed, 0) * qtd AS sobre
        FROM base
    )
    SELECT * FROM calc
    WHERE preco_bps > preco_cmed * (1 + CAST(:tol AS numeric) / 100.0) AND sobre >= CAST(:minv AS numeric)
    ORDER BY sobre DESC
    LIMIT :lim
    """
    stmt = text(sql)
    if binds:
        stmt = stmt.bindparams(*binds)
    with _engine.connect() as conn:
        df = pd.read_sql(stmt, conn, params=params)
    for c in ("preco_bps", "preco_cmed", "delta", "sobre", "qtd"):
        df[c] = pd.to_numeric(df[c]).astype(float)
    df["data_compra"] = pd.to_datetime(df["data_compra"])
    return df


@st.cache_data(show_spinner="Calculando a evolução mensal...", ttl=600)
def consultar_mensal(_engine, conn_id, schema, m, d1, d2, tol, minv, ufs_raw, pares):
    F, P, join_cal, dt = partes_sql(m, schema)
    qtd = f"COALESCE({sql_num('f.' + q(m['qtd']))}, 1)" if m["qtd"] else "1"
    params = {"d1": d1, "d2": d2, "tol": float(tol), "minv": float(minv)}
    filtros, binds = [], []
    if ufs_raw:
        filtros.append(f"f.{q(m['uf'])} IN :ufs")
        params["ufs"] = list(ufs_raw)
        binds.append(bindparam("ufs", expanding=True))
    if pares:
        conds = []
        for i, (u, mn) in enumerate(pares):
            conds.append(
                f"(f.{q(m['uf'])} IS NOT DISTINCT FROM :u{i} AND f.{q(m['mun'])} IS NOT DISTINCT FROM :m{i})"
            )
            params[f"u{i}"], params[f"m{i}"] = u, mn
        filtros.append("(" + " OR ".join(conds) + ")")
    extra = (" AND " + " AND ".join(filtros)) if filtros else ""
    sql = f"""
    WITH {sql_referencia(m, schema)}, base AS (
        SELECT {dt} AS data_compra, {sql_num('f.' + q(m['preco']))} AS preco_bps, {qtd} AS qtd, p.preco_cmed
        FROM {F} f
        JOIN icms ic ON upper(trim(CAST(f.{q(m['uf'])} AS text))) = ic.estado
        JOIN ref p ON {sql_reg('f.' + q(m['reg']))} = p.reg AND p.icms_pct = ic.icms_pct
        {join_cal}
        WHERE {sql_num('f.' + q(m['preco']))} IS NOT NULL AND {dt} BETWEEN :d1 AND :d2 {extra}
    ), calc AS (
        SELECT date_trunc('month', data_compra) AS mes,
               preco_bps > preco_cmed * (1 + CAST(:tol AS numeric) / 100.0) AS suspeita,
               GREATEST(preco_bps - preco_cmed, 0) * qtd AS sobre
        FROM base
    )
    SELECT mes,
           COUNT(*) FILTER (WHERE suspeita AND sobre >= CAST(:minv AS numeric)) AS n_susp,
           COALESCE(SUM(sobre) FILTER (WHERE suspeita AND sobre >= CAST(:minv AS numeric)), 0) AS sobrepreco
    FROM calc
    GROUP BY mes
    ORDER BY mes
    """
    stmt = text(sql)
    if binds:
        stmt = stmt.bindparams(*binds)
    with _engine.connect() as conn:
        df = pd.read_sql(stmt, conn, params=params)
    df["mes"] = pd.to_datetime(df["mes"])
    df["n_susp"] = pd.to_numeric(df["n_susp"]).astype(float)
    df["sobrepreco"] = pd.to_numeric(df["sobrepreco"]).astype(float)
    return df


# ----------------------------------------------------------------------------
# SIDEBAR: conexão e mapeamento de colunas
# ----------------------------------------------------------------------------
with st.sidebar:
    st.header("Conexão")
    usuario = st.text_input("Usuário", PG_USER_PADRAO)
    senha = st.text_input("Senha", PG_SENHA_PADRAO, type="password")
    schema = st.text_input("Schema", PG_SCHEMA_PADRAO)
    st.header("Regra")
    tolerancia = st.number_input("Tolerância sobre a PMVG (%)", 0.0, 100.0, TOLERANCIA_PADRAO, 0.5)

conn_id = f"{usuario}@{PG_HOST}:{PG_PORT}/{PG_DB}"
engine = obter_engine(usuario, senha, PG_HOST, PG_PORT, PG_DB)

try:
    estrutura = ler_estrutura(engine, conn_id, schema)
except Exception as e:  # noqa: BLE001
    st.error(f"Não consegui conectar ao PostgreSQL ({conn_id}). Confira usuário e senha na barra lateral.\n\n{e}")
    st.stop()

faltam = [t for t in (T_FATO, T_PMVG, T_CAL, T_ICMS) if t not in set(estrutura["table_name"])]
if faltam:
    st.error(f"Tabelas não encontradas no schema '{schema}': {', '.join(faltam)}")
    st.stop()


def colunas(tabela):
    d = estrutura[estrutura["table_name"] == tabela]
    return d["column_name"].tolist(), dict(zip(d["column_name"], d["data_type"]))


f_cols, f_tipos = colunas(T_FATO)
p_cols, _ = colunas(T_PMVG)
c_cols, c_tipos = colunas(T_CAL)
i_cols, _ = colunas(T_ICMS)


def escolher(rotulo, opcoes, padrao, key):
    idx = opcoes.index(padrao) if padrao in opcoes else 0
    return st.selectbox(rotulo, opcoes, index=idx, key=key)


# colunas "PMVG 17 %", "PMVG 20,5 %" etc. da dim_pmvg (uma por alíquota de ICMS)
pmvg_cols = []
for c in p_cols:
    mt = re.fullmatch(r"pmvg\s*(\d+(?:[.,]\d+)?)\s*%", norm(c))
    if mt:
        pmvg_cols.append((c, float(mt.group(1).replace(",", "."))))

# padrões para a data / calendário: fato_bps.dt_compra = dim_calendario.data
chaves_fato = {chave(c): c for c in f_cols}
fato_datas = [c for c in f_cols if f_tipos.get(c) in TIPOS_DATA]
cal_datas = [c for c in c_cols if c_tipos.get(c) in TIPOS_DATA]
fk_cfg = detectar(f_cols, ["dt_compra"])
ck_cfg = detectar(c_cols, ["data"])
if fk_cfg and ck_cfg:
    fk_pad, cal_key_pad, cal_data_pad = fk_cfg, ck_cfg, ck_cfg
else:  # plano B: procura uma chave em comum
    comuns = [c for c in c_cols if chave(c) in chaves_fato]
    pref = [c for c in comuns if re.search(r"(sk|id|cd|key|chave)", norm(c))]
    cal_key_pad = (pref or comuns or [None])[0]
    fk_pad = (
        chaves_fato.get(chave(cal_key_pad)) if cal_key_pad
        else (detectar(f_cols, CAND["f_data"]) or (fato_datas[0] if fato_datas else f_cols[0]))
    )
    cal_data_pad = next((c for c in cal_datas if re.search(r"(data|date|dt)", norm(c))), None) or (
        cal_datas[0] if cal_datas else detectar(c_cols, CAND["c_data"])
    )

with st.sidebar:
    with st.expander("Mapeamento de colunas", expanded=False):
        st.caption(f"Tabela {T_FATO}")
        m_reg = escolher("Registro Anvisa", f_cols, detectar(f_cols, CAND["f_registro"]), "m_reg")
        m_preco = escolher("Preço unitário pago", f_cols, detectar(f_cols, CAND["f_preco"]), "m_preco")
        m_mun = escolher("Município", f_cols, detectar(f_cols, CAND["f_municipio"]), "m_mun")
        m_uf = escolher("UF (sigla, nome ou código IBGE)", f_cols, detectar(f_cols, CAND["f_uf"]), "m_uf")
        m_qtd = escolher("Quantidade", [NENHUMA] + f_cols, detectar(f_cols, CAND["f_qtd"]) or NENHUMA, "m_qtd")
        m_idc = escolher("ID da compra", f_cols, detectar(f_cols, CAND["f_id"]), "m_idc")
        m_inst = escolher("Ente comprador", f_cols, detectar(f_cols, CAND["f_inst"]), "m_inst")
        m_forn = escolher("Fornecedor", f_cols, detectar(f_cols, CAND["f_forn"]), "m_forn")
        st.caption(f"Data / {T_CAL}")
        m_fk = escolher("Coluna de data ou chave (na fato)", f_cols, fk_pad, "m_fk")
        m_calk = escolher("Chave no calendário", [NENHUMA] + c_cols, cal_key_pad or NENHUMA, "m_calk")
        if m_calk == NENHUMA:
            m_dt = escolher("Coluna de data (na fato)", f_cols, m_fk, "m_dt_f")
        else:
            m_dt = escolher("Coluna de data (no calendário)", c_cols, cal_data_pad, "m_dt_c")
        st.caption(f"Tabela {T_PMVG}")
        m_preg = escolher("Registro", p_cols, detectar(p_cols, CAND["p_registro"]), "m_preg")
        m_pqtd = escolher("Quantidade da apresentação (QUANTIDADE)", p_cols, detectar(p_cols, CAND["p_qtd"]), "m_pqtd")
        m_subst = escolher("Nome do medicamento (substância)", p_cols, detectar(p_cols, CAND["p_subst"]), "m_subst")
        st.caption(f"Tabela {T_ICMS}")
        m_iest = escolher("Estado", i_cols, detectar(i_cols, CAND["i_estado"]), "m_iest")
        m_iicms = escolher("Alíquota de ICMS", i_cols, detectar(i_cols, CAND["i_icms"]), "m_iicms")

mapa_cols = {
    "reg": m_reg, "preco": m_preco, "mun": m_mun, "uf": m_uf,
    "qtd": None if m_qtd == NENHUMA else m_qtd,
    "fk": m_fk, "cal_key": None if m_calk == NENHUMA else m_calk, "dt": m_dt,
    "p_reg": m_preg, "p_qtd": m_pqtd, "i_estado": m_iest, "i_icms": m_iicms, "pmvg_cols": tuple(pmvg_cols),
    "id": m_idc, "inst": m_inst, "forn": m_forn, "subst": m_subst,
    "juntar_como_data": m_calk != NENHUMA and f_tipos.get(m_fk) in TIPOS_DATA and c_tipos.get(m_calk) in TIPOS_DATA,
}

if not pmvg_cols:
    st.error(f"Não encontrei colunas no formato 'PMVG 17 %' na tabela {T_PMVG}. Colunas disponíveis: {', '.join(p_cols)}")
    st.stop()

if mapa_cols["qtd"] is None:
    st.warning(
        "Sem coluna de quantidade: o sobrepreço está sendo somado só por unidade "
        "(preço BPS − preço PMVG). Escolha a coluna em 'Mapeamento de colunas' na barra lateral."
    )

# ----------------------------------------------------------------------------
# DADOS
# ----------------------------------------------------------------------------
try:
    dmin, dmax, locais = ler_metadados(engine, conn_id, schema, mapa_cols)
except Exception as e:  # noqa: BLE001
    st.error(f"Erro ao ler as tabelas. Revise o mapeamento de colunas na barra lateral.\n\n{e}")
    st.stop()

geo = carregar_geo()
ufs_df = pd.DataFrame(geo["ufs"])
mapa_uf = {}
for _, r in ufs_df.iterrows():
    mapa_uf[r["codarea"]] = r["sigla"]
    mapa_uf[norm(r["sigla"])] = r["sigla"]
    mapa_uf[norm(r["nome"])] = r["sigla"]


def resolver_uf(valor):
    if pd.isna(valor):
        return None
    v = str(valor).strip()
    if re.fullmatch(r"\d{2,}", v):
        return mapa_uf.get(v[:2])
    return mapa_uf.get(norm(v))


locais["uf_sigla"] = locais["uf"].map(resolver_uf)
locais["rotulo"] = nome_municipio(locais["municipio"]) + locais["uf_sigla"].map(lambda s: f" ({s})" if s else "")

st.markdown('<h1 class="titulo">Análise de Sobrepreço em Compras Públicas</h1>', unsafe_allow_html=True)

# ----------------------------------------------------------------------------
# FILTROS
# ----------------------------------------------------------------------------
if "versao" not in st.session_state:
    st.session_state["versao"] = 0
pend = st.session_state.pop("pendente", None)  # seleção feita clicando num gráfico
if pend:
    st.session_state.update(pend)


def limpar_filtros():
    st.session_state["f_uf"] = []
    st.session_state["f_mun"] = []
    st.session_state["versao"] += 1


def pontos_selecionados(evento):
    try:
        return list(evento["selection"]["points"])
    except Exception:  # noqa: BLE001
        return []


def aplicar_selecao(chave_grafico, ids, atuais, destino):
    """Clique num gráfico -> filtra o painel inteiro. Clicar de novo no mesmo item limpa o filtro."""
    ids = [i for i in ids if i]
    sig = tuple(sorted(ids))
    if not sig or st.session_state.get("ult_" + chave_grafico) == sig:
        return
    st.session_state["ult_" + chave_grafico] = sig
    novo = [] if set(ids) == set(atuais) else sorted(ids)
    st.session_state["pendente"] = destino(novo)
    st.session_state["versao"] += 1
    st.rerun()


f1, f2, f3, f4 = st.columns([1, 1.3, 1.3, 1])
with f1:
    ufs_opts = sorted(locais["uf_sigla"].dropna().unique())
    st.session_state["f_uf"] = [u for u in st.session_state.get("f_uf", []) if u in ufs_opts]
    sel_uf = st.multiselect("Estado", ufs_opts, placeholder="Todos", key="f_uf")
with f2:
    base_mun = locais[locais["uf_sigla"].isin(sel_uf)] if sel_uf else locais
    mun_opts = sorted(base_mun["rotulo"].unique())
    st.session_state["f_mun"] = [m for m in st.session_state.get("f_mun", []) if m in mun_opts]
    sel_mun = st.multiselect("Município", mun_opts, placeholder="Todos", key="f_mun")
with f3:
    d_ini = pd.to_datetime(dmin).date() if pd.notna(dmin) else date.today()
    d_fim = pd.to_datetime(dmax).date() if pd.notna(dmax) else date.today()
    periodo = st.date_input("Período", (d_ini, d_fim), min_value=d_ini, max_value=d_fim, format="DD/MM/YYYY")
with f4:
    valor_min = st.number_input("Valor Mínimo do Sobrepreço (R$)", min_value=0.0, value=0.0, step=100.0)

if sel_uf or sel_mun:
    st.button("Limpar seleção", on_click=limpar_filtros)

if not (isinstance(periodo, (tuple, list)) and len(periodo) == 2):
    st.info("Selecione a data inicial e a final do período.")
    st.stop()

try:
    df = consultar(engine, conn_id, schema, mapa_cols, periodo[0], periodo[1], tolerancia, valor_min)
except Exception as e:  # noqa: BLE001
    st.error(f"Erro na consulta ao banco. Revise o mapeamento de colunas na barra lateral.\n\n{e}")
    st.stop()

df["uf_sigla"] = df["uf"].map(resolver_uf)
df["rotulo"] = nome_municipio(df["municipio"]) + df["uf_sigla"].map(lambda s: f" ({s})" if s else "")
if sel_uf:
    df = df[df["uf_sigla"].isin(sel_uf)]
if sel_mun:
    df = df[df["rotulo"].isin(sel_mun)]

analisadas = int(df["n_total"].sum())
n_susp = int(df["n_susp"].sum())
total_sobre = float(df["sobrepreco"].sum())

# ----------------------------------------------------------------------------
# KPIs + RANKING (município) + MAPA (estado)
# ----------------------------------------------------------------------------
col_esq, col_dir = st.columns([1.1, 1])

with col_esq:
    k1, k2 = st.columns(2)
    k1.markdown(
        f'<div class="card"><div class="rotulo">Total de Compras com Sobrepreço</div>'
        f'<div class="valor"><span class="icone">⚠</span>{n_susp:,}</div></div>'.replace(",", "."),
        unsafe_allow_html=True,
    )
    k2.markdown(
        f'<div class="card"><div class="rotulo">Sobrepreço Total (R$)</div>'
        f'<div class="valor"><span class="icone">$</span>{brl_curto(total_sobre)}</div></div>',
        unsafe_allow_html=True,
    )
    pct = (n_susp / analisadas * 100) if analisadas else 0
    st.caption(
        f"{analisadas:,} compras com preço de referência na PMVG · "
        f"{pct:.1f}% acima de {tolerancia:g}% da PMVG".replace(",", ".")
    )

    st.markdown('<div class="painel-titulo">Ranking de Sobrepreço por Município (R$)</div>', unsafe_allow_html=True)
    top = df[df["n_susp"] > 0].sort_values("sobrepreco", ascending=False).iloc[::-1]
    if top.empty:
        st.info("Nenhuma compra suspeita com os filtros atuais.")
    else:
        fig_bar = go.Figure(
            go.Bar(
                x=top["sobrepreco"], y=top["rotulo"], orientation="h",
                marker=dict(color=top["sobrepreco"], colorscale=[[0, "#f9a85a"], [0.5, "#e2553f"], [1, "#a31224"]]),
                text=[brl_barra(v) for v in top["sobrepreco"]], textposition="outside", cliponaxis=False,
                hovertemplate="%{y}<br>%{text}<extra></extra>",
            )
        )
        fig_bar.update_layout(
            height=34 * len(top) + 30, margin=dict(l=0, r=70, t=10, b=0), bargap=0.3,
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
            font=dict(color="#e8eaf0", size=14), xaxis=dict(visible=False), yaxis=dict(automargin=True),
        )
        # lista rolável: mostra todos os municípios com sobrepreço, do maior para o menor
        with st.container(height=min(640, 34 * len(top) + 40), border=False):
            ev_bar = st.plotly_chart(
                fig_bar, use_container_width=True, key=f"bar_{st.session_state['versao']}",
                on_select="rerun", selection_mode="points",
            )
        aplicar_selecao(
            f"bar_{st.session_state['versao']}",
            [p.get("y") for p in pontos_selecionados(ev_bar)],
            sel_mun, lambda novo: {"f_mun": novo},
        )

with col_dir:
    st.markdown('<span class="tag-vermelha">Sobrepreços por Estado</span>', unsafe_allow_html=True)
    por_uf = (
        df.dropna(subset=["uf_sigla"]).groupby("uf_sigla")
        .agg(sobrepreco=("sobrepreco", "sum"), compras=("n_susp", "sum")).reset_index()
    )
    est = ufs_df.merge(por_uf, left_on="sigla", right_on="uf_sigla", how="left").fillna({"sobrepreco": 0, "compras": 0})
    bol = est[est["compras"] > 0].copy()
    bol["hover"] = [
        f"<b>{r.nome}</b><br>Compras com sobrepreço: {fmt_int(r.compras)}<br>Sobrepreço total: {brl_completo(r.sobrepreco)}"
        for r in bol.itertuples()
    ]

    usa_map = hasattr(go, "Choroplethmap")
    Coropleto = go.Choroplethmap if usa_map else go.Choroplethmapbox
    Dispersao = go.Scattermap if usa_map else go.Scattermapbox
    escala = [[0, "#f9a85a"], [0.5, "#e2553f"], [1, "#a31224"]]
    # retângulo escuro cobrindo o mundo: esconde países vizinhos e oceano, deixando só o Brasil
    fundo = {
        "type": "FeatureCollection",
        "features": [{"type": "Feature", "properties": {}, "geometry": {
            "type": "Polygon", "coordinates": [[[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]]]}}],
    }

    fig = go.Figure()
    fig.add_trace(
        Coropleto(
            geojson=geo["geojson"], locations=est["codarea"], featureidkey="properties.codarea",
            z=[1] * len(est), colorscale=[[0, "#3b4252"], [1, "#3b4252"]], showscale=False,
            hoverinfo="skip", marker=dict(line=dict(color="#0f1523", width=0.8), opacity=1),
        )
    )
    if not bol.empty:
        tam = 24 + 56 * (bol["sobrepreco"] / bol["sobrepreco"].max()) ** 0.5
        fig.add_trace(
            Dispersao(
                lon=bol["lon"], lat=bol["lat"], mode="markers+text",
                text=bol["compras"].astype(int).astype(str), textposition="middle center",
                textfont=dict(color="#ffffff", size=12),
                marker=dict(size=tam, color=bol["sobrepreco"], colorscale=escala, opacity=0.9),
                customdata=list(zip(bol["hover"], bol["sigla"])), hovertemplate="%{customdata[0]}<extra></extra>",
            )
        )
    vista = dict(
        style="white-bg", center=dict(lat=-14.5, lon=-52.5), zoom=3.1,
        layers=[dict(sourcetype="geojson", source=fundo, type="fill", color="#0f1523", below="traces")],
    )
    fig.update_layout(**({"map": vista} if usa_map else {"mapbox": vista}))
    fig.update_layout(
        height=680, margin=dict(l=0, r=0, t=10, b=0), paper_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e8eaf0"), showlegend=False,
    )
    ev_mapa = st.plotly_chart(
        fig, use_container_width=True, key=f"mapa_{st.session_state['versao']}",
        on_select="rerun", selection_mode="points",
    )
    siglas_clicadas = []
    for p in pontos_selecionados(ev_mapa):
        cd = p.get("customdata")
        if isinstance(cd, (list, tuple)) and len(cd) > 1:
            siglas_clicadas.append(cd[1])
    aplicar_selecao(
        f"mapa_{st.session_state['versao']}", siglas_clicadas,
        sel_uf, lambda novo: {"f_uf": novo, "f_mun": []},
    )
    sem_uf = int(df.loc[df["uf_sigla"].isna(), "n_susp"].sum())
    if sem_uf:
        st.caption(f"{sem_uf:,} compras suspeitas sem UF reconhecida (não aparecem no mapa). "
                   "Confira a coluna de UF no mapeamento.".replace(",", "."))


# ----------------------------------------------------------------------------
# EVOLUÇÃO MENSAL + TABELA DETALHADA (respeitam todos os filtros e seleções do painel)
# ----------------------------------------------------------------------------
LIMITE_TABELA = 20000


def _py(v):
    return v.item() if hasattr(v, "item") else v


ufs_raw = ()
if sel_uf:
    ufs_raw = tuple(sorted({str(_py(v)) for v in locais.loc[locais["uf_sigla"].isin(sel_uf), "uf"].dropna()}))
pares = ()
if sel_mun:
    sub = locais[locais["rotulo"].isin(sel_mun)]
    pares = tuple(sorted({(_py(u), _py(mn)) for u, mn in zip(sub["uf"], sub["municipio"])}, key=str))

# ---- gráfico de linhas: progressão mensal dos desvios
st.markdown('<div class="painel-titulo" style="margin-top:1.5rem">Evolução Mensal dos Desvios</div>',
            unsafe_allow_html=True)
try:
    mensal = consultar_mensal(
        engine, conn_id, schema, mapa_cols, periodo[0], periodo[1], tolerancia, valor_min, ufs_raw, pares
    )
except Exception as e:  # noqa: BLE001
    st.error(f"Erro ao calcular a evolução mensal. Revise o mapeamento de colunas na barra lateral.\n\n{e}")
else:
    if mensal.empty:
        st.info("Sem compras no período e nos filtros selecionados.")
    else:
        meses = pd.date_range(mensal["mes"].min(), mensal["mes"].max(), freq="MS")
        mensal = mensal.set_index("mes").reindex(meses, fill_value=0).rename_axis("mes").reset_index()
        fig_m = go.Figure()
        fig_m.add_trace(
            go.Scatter(
                x=mensal["mes"], y=mensal["n_susp"], name="Compras com sobrepreço", mode="lines+markers",
                line=dict(color="#f9a85a", width=3), marker=dict(size=8), yaxis="y",
                customdata=[fmt_int(v) for v in mensal["n_susp"]],
                hovertemplate="Compras com sobrepreço: %{customdata}<extra></extra>",
            )
        )
        fig_m.add_trace(
            go.Scatter(
                x=mensal["mes"], y=mensal["sobrepreco"], name="Sobrepreço total (R$)", mode="lines+markers",
                line=dict(color="#e03131", width=3), marker=dict(size=8), yaxis="y2",
                customdata=[brl_completo(v) for v in mensal["sobrepreco"]],
                hovertemplate="Sobrepreço total: %{customdata}<extra></extra>",
            )
        )
        fig_m.update_layout(
            height=380, margin=dict(l=10, r=10, t=10, b=10), hovermode="x unified",
            paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font=dict(color="#e8eaf0", size=13),
            legend=dict(orientation="h", y=1.12, x=0, font=dict(color="#e8eaf0")),
            xaxis=dict(tickformat="%m/%Y", hoverformat="%m/%Y", gridcolor="#2a3142", showline=False),
            yaxis=dict(
                title=dict(text="Compras com sobrepreço", font=dict(color="#f9a85a")),
                tickfont=dict(color="#f9a85a"), gridcolor="#2a3142", rangemode="tozero",
            ),
            yaxis2=dict(
                title=dict(text="Sobrepreço total (R$)", font=dict(color="#e03131")),
                tickfont=dict(color="#e03131"), overlaying="y", side="right", showgrid=False,
                rangemode="tozero", tickformat="~s",
            ),
        )
        st.plotly_chart(fig_m, use_container_width=True)

# ---- tabela detalhada
st.markdown('<div class="painel-titulo" style="margin-top:1.5rem">Detalhamento das compras com sobrepreço</div>',
            unsafe_allow_html=True)


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def _mix(c1, c2, t):
    a, b = _hex(c1), _hex(c2)
    return "#%02x%02x%02x" % tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def fmt_pct(v):
    return "" if pd.isna(v) else f"{v:,.1f}%".replace(",", "X").replace(".", ",").replace("X", ".")


try:
    det = consultar_detalhe(
        engine, conn_id, schema, mapa_cols, periodo[0], periodo[1], tolerancia, valor_min, ufs_raw, pares, LIMITE_TABELA
    )
except Exception as e:  # noqa: BLE001
    st.error(f"Erro ao buscar o detalhamento. Revise o mapeamento de colunas na barra lateral.\n\n{e}")
else:
    aviso = f" (limitado às {fmt_int(LIMITE_TABELA)} de maior sobrepreço)" if n_susp > len(det) else ""
    st.caption(f"Exibindo {fmt_int(len(det))} de {fmt_int(n_susp)} compras com sobrepreço, "
               f"ordenadas pelo maior sobrepreço total{aviso}.")
    pct = ((det["preco_bps"] / det["preco_cmed"] - 1) * 100).replace([float("inf"), float("-inf")], float("nan"))
    tabela = pd.DataFrame(
        {
            "Medicamento": det["substancia"],
            "ID da compra": det["id_compra"],
            "Valor de compra (R$)": det["preco_bps"],
            "Valor máximo ANVISA (R$)": det["preco_cmed"],
            "Sobrepreço (R$)": det["delta"],
            "Sobrepreço (%)": pct,
            "Ente comprador": det["ente"],
            "Fornecedor": det["fornecedor"],
            "Estado": det["uf"],
            "Cidade": nome_municipio(det["municipio"]),
            "Data da compra": det["data_compra"],
            "Quantidade": det["qtd"],
            "Sobrepreço total (R$)": det["sobre"],
        }
    )
    # quanto maior o %, mais vermelho o fundo e o texto da célula
    topo = max(50.0, float(tabela["Sobrepreço (%)"].quantile(0.9))) if tabela["Sobrepreço (%)"].notna().any() else 100.0

    def cor_pct(v):
        if pd.isna(v):
            return ""
        t = min(max(v / topo, 0.0), 1.0)
        return (f"background-color: {_mix('#232a3b', '#7a1420', t)}; "
                f"color: {_mix('#cfd3de', '#ff5c5c', t)}; font-weight: 600")

    estilo = tabela.style.format({"Sobrepreço (%)": fmt_pct})
    estilo = (estilo.map if hasattr(estilo, "map") else estilo.applymap)(cor_pct, subset=["Sobrepreço (%)"])
    st.dataframe(
        estilo, hide_index=True, use_container_width=True, height=460,
        column_config={
            "Valor de compra (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Valor máximo ANVISA (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Sobrepreço (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Sobrepreço total (R$)": st.column_config.NumberColumn(format="R$ %.2f"),
            "Quantidade": st.column_config.NumberColumn(format="%.0f"),
            "Data da compra": st.column_config.DateColumn(format="DD/MM/YYYY"),
        },
    )
    st.download_button(
        "Baixar tabela (CSV)", tabela.to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
        file_name="compras_com_sobrepreco.csv", mime="text/csv",
    )
