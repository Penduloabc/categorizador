"""
Validador de vocabulario (sustantivos, adjetivos y otras categorías) — DLE (RAE)
Versión GitHub Actions: recibe el TXT a procesar como argumento, autentica con
Service Account (sin flujo interactivo) y reutiliza intacta la lógica de
scraping, extracción y progreso del notebook original.

Uso:
    python validador_vocabulario_rae.py --archivo vocabulario_general_2.txt
    python validador_vocabulario_rae.py --archivo-id 1AbCdEfGhIjKlMnOpQrStUvWxYz

Variables de entorno esperadas:
    GOOGLE_SERVICE_ACCOUNT_JSON  -> contenido completo del JSON de la Service Account
    CARPETA_ID                   -> ID de la carpeta de Drive que contiene los .txt
                                     (opcional si se usa --archivo-id directamente)
"""

import os
import re
import io
import json
import time
import random
import argparse
import datetime
from urllib.parse import quote

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread


# ============================================================
# 0. ARGUMENTOS
# ============================================================
parser = argparse.ArgumentParser()
parser.add_argument('--archivo', help='Nombre del .txt a procesar (ej. vocabulario_general_2.txt)')
parser.add_argument('--archivo-id', help='ID de Drive del .txt a procesar (alternativa a --archivo)')
args = parser.parse_args()

if not args.archivo and not args.archivo_id:
    raise SystemExit('Debes pasar --archivo <nombre> o --archivo-id <id>')


# ============================================================
# 1. RUTAS / PREFIJOS (idéntico al notebook original)
# ============================================================
PREFIJO_TXT  = "vocabulario_general_"
PREFIJO_HOJA = "Vocabulario general "

CARPETA_ID = os.environ.get('CARPETA_ID')


# ============================================================
# 2. COMPORTAMIENTO HUMANO (idéntico al notebook original)
# ============================================================
def pausa(base=1.5, extra=2.0):
    time.sleep(base + random.uniform(0, extra))

def pausa_larga():
    t = random.uniform(8, 18)
    print(f'  [pausa larga: {t:.1f}s]')
    time.sleep(t)

def scroll_aleatorio(driver):
    px = random.randint(80, 500)
    driver.execute_script(f'window.scrollBy(0, {px});')
    time.sleep(random.uniform(0.2, 0.6))


# ============================================================
# 3. NAVEGADOR (idéntico al notebook original)
# ============================================================
def crear_driver():
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    options.add_argument('--disable-gpu')
    options.add_argument('--window-size=1366,768')
    options.add_argument(
        'user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36'
    )
    options.add_experimental_option('excludeSwitches', ['enable-automation'])
    options.add_experimental_option('useAutomationExtension', False)
    options.add_argument('--disable-blink-features=AutomationControlled')

    driver = webdriver.Chrome(options=options)
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
        'source': "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    })
    return driver


# ============================================================
# 4. AUTENTICACIÓN — ÚNICO BLOQUE REALMENTE NUEVO
#    Reemplaza auth.authenticate_user() + drive.mount() por Service Account.
# ============================================================
SCOPES = [
    'https://www.googleapis.com/auth/drive',
    'https://www.googleapis.com/auth/spreadsheets',
]

_sa_json = os.environ.get('GOOGLE_SERVICE_ACCOUNT_JSON')
if not _sa_json:
    raise SystemExit('Falta la variable de entorno GOOGLE_SERVICE_ACCOUNT_JSON')

_sa_info = json.loads(_sa_json)
creds = service_account.Credentials.from_service_account_info(_sa_info, scopes=SCOPES)

gc            = gspread.authorize(creds)
drive_service = build('drive', 'v3', credentials=creds)
print('✔ Autenticación completada (Service Account).')


# ============================================================
# 4.1 WRAPPER DE REINTENTOS PARA LLAMADAS A LA API (Sheets/Drive)
#     Debe definirse ANTES de cualquier llamada a Drive/Sheets, ya que
#     las secciones 5 y 6 (localizar TXT, crear/leer la hoja) también
#     lo usan.
# ============================================================
_ERRORES_TRANSITORIOS = (
    '429', '500', '502', '503', '504',
    'RemoteDisconnected', 'ConnectionError', 'ConnectionAborted',
    'Connection aborted', 'Timeout', 'timed out', 'ReadTimeout',
    'ProtocolError', 'BrokenPipeError', 'ServerNotFoundError',
)


def _api_call(fn, max_reintentos=5, espera_inicial=15, etiqueta='API'):
    """
    Wrapper genérico de reintentos con backoff exponencial. Cubre TANTO
    lecturas como escrituras contra Sheets/Drive (get_all_values, update,
    append_row/append_rows, files().get/list, etc.), a diferencia del
    _sheets_write original que solo protegía escrituras. Reintenta ante
    errores transitorios de red o cuota; cualquier otro error se relanza
    de inmediato (no queremos ocultar errores reales de lógica).
    """
    espera = espera_inicial
    for intento in range(max_reintentos + 1):
        try:
            return fn()
        except Exception as e:
            es_transitorio = any(marca in str(e) or marca in type(e).__name__
                                  for marca in _ERRORES_TRANSITORIOS)
            if es_transitorio and intento < max_reintentos:
                print(f'  [{etiqueta}] Error transitorio ({type(e).__name__}). '
                      f'Esperando {espera}s (intento {intento+1}/{max_reintentos})...')
                time.sleep(espera)
                espera *= 2
            else:
                raise


# ============================================================
# 5. LOCALIZAR EL .TXT — vía API, explícito por nombre o ID
#    (en Actions no hay "carpeta propia por cuenta"; el archivo llega
#    como parámetro de la matriz del workflow)
# ============================================================
if args.archivo_id:
    _meta = _api_call(lambda: drive_service.files().get(fileId=args.archivo_id, fields='id, name').execute(),
                       etiqueta='localizar_por_id')
    _ARCHIVO_TXT_ID = _meta['id']
    NOMBRE_ARCHIVO  = _meta['name']
else:
    if not CARPETA_ID:
        raise SystemExit('Falta CARPETA_ID en el entorno (requerido al usar --archivo por nombre)')
    _resp_txt = _api_call(lambda: drive_service.files().list(
        q=(
            f"'{CARPETA_ID}' in parents and name='{args.archivo}' "
            "and trashed=false"
        ),
        fields='files(id, name)',
    ).execute(), etiqueta='localizar_por_nombre')
    _archivos = _resp_txt.get('files', [])
    if not _archivos:
        raise FileNotFoundError(f'No se encontró "{args.archivo}" en la carpeta {CARPETA_ID}')
    _ARCHIVO_TXT_ID = _archivos[0]['id']
    NOMBRE_ARCHIVO  = _archivos[0]['name']

_num_match  = re.search(rf'{re.escape(PREFIJO_TXT)}(\w+)\.txt', NOMBRE_ARCHIVO)
_num        = _num_match.group(1) if _num_match else NOMBRE_ARCHIVO
NOMBRE_HOJA = f'{PREFIJO_HOJA}{_num}'

# Averiguar la carpeta contenedora real del archivo (para crear/ubicar la hoja ahí)
_meta_parents = _api_call(lambda: drive_service.files().get(fileId=_ARCHIVO_TXT_ID, fields='parents').execute(),
                           etiqueta='meta_parents')
CARPETA_ID = (_meta_parents.get('parents') or [CARPETA_ID])[0]

# Descargar el .txt a un archivo local temporal
RUTA_TXT = f'/tmp/{NOMBRE_ARCHIVO}'


def _descargar_txt():
    _request = drive_service.files().get_media(fileId=_ARCHIVO_TXT_ID)
    with io.FileIO(RUTA_TXT, 'wb') as _fh:
        _downloader = MediaIoBaseDownload(_fh, _request)
        _done = False
        while not _done:
            _, _done = _downloader.next_chunk()


_api_call(_descargar_txt, etiqueta='descargar_txt')

print(f'  Archivo asignado : {NOMBRE_ARCHIVO}')
print(f'  Hoja de cálculo  : {NOMBRE_HOJA}')


# ============================================================
# 6. HOJA DE CÁLCULO Y PESTAÑA DE PROGRESO (idéntico al notebook original)
# ============================================================
_resp_hojas = _api_call(lambda: drive_service.files().list(
    q=(
        f"'{CARPETA_ID}' in parents and name='{NOMBRE_HOJA}' "
        "and mimeType='application/vnd.google-apps.spreadsheet' "
        "and trashed=false"
    ),
    fields='files(id, name)',
).execute(), etiqueta='listar_hojas')
_hojas_existentes = _resp_hojas.get('files', [])

if _hojas_existentes:
    sh = _api_call(lambda: gc.open_by_key(_hojas_existentes[0]['id']), etiqueta='abrir_hoja')
    print(f'✔ Hoja existente reutilizada: {NOMBRE_HOJA}')
else:
    sh = _api_call(lambda: gc.create(NOMBRE_HOJA), etiqueta='crear_hoja')
    _file_id = sh.id
    _meta = _api_call(lambda: drive_service.files().get(fileId=_file_id, fields='parents').execute(),
                       etiqueta='meta_hoja')
    _padres_actuales = ','.join(_meta.get('parents', []))
    _api_call(lambda: drive_service.files().update(
        fileId=_file_id,
        addParents=CARPETA_ID,
        removeParents=_padres_actuales,
        fields='id, parents',
    ).execute(), etiqueta='mover_hoja')
    print(f'✔ Hoja creada y movida a la carpeta del proyecto: {NOMBRE_HOJA}')

worksheet = sh.sheet1
valores = _api_call(lambda: worksheet.get_all_values(), etiqueta='leer_headers')
hoja_vacia = (not valores) or all(not any(fila) for fila in valores)
if hoja_vacia:
    _api_call(lambda: worksheet.append_row(['Palabra', 'Enlace', 'Abreviaturas', 'Sinónimos', 'Antónimos']),
              etiqueta='crear_headers')
    print('Encabezados creados (5 columnas).')
else:
    print('La hoja ya tenía contenido; no se sobrescriben encabezados.')

try:
    progreso_ws = _api_call(lambda: sh.worksheet('Progreso'), max_reintentos=2, etiqueta='buscar_progreso')
except gspread.WorksheetNotFound:
    progreso_ws = _api_call(lambda: sh.add_worksheet(title='Progreso', rows=5, cols=2), etiqueta='crear_progreso')
    _api_call(lambda: progreso_ws.append_row(['ultimo_indice', 'descripcion']), etiqueta='header_progreso')
    _api_call(lambda: progreso_ws.append_row([0, 'Inicio']), etiqueta='init_progreso')

print('Conectado a la hoja:', sh.title)


# ============================================================
# 7. CANDIDATOS + FUNCIONES DE EXTRACCIÓN (idéntico al notebook original)
# ============================================================
with open(RUTA_TXT, encoding='utf-8') as f:
    candidatos = [l.strip() for l in f if l.strip()]

print(f'Total de candidatos cargados desde {NOMBRE_ARCHIVO}: {len(candidatos)}')
print('Primeros 5:', candidatos[:5])
print('Últimos 5: ', candidatos[-5:])


def leer_progreso(progreso_ws):
    vals = _api_call(lambda: progreso_ws.get_all_values(), etiqueta='leer_progreso')
    if len(vals) < 2 or not vals[1][0]:
        return 0
    return int(vals[1][0])


def guardar_progreso(progreso_ws, indice, forzar=False, cada=10):
    if forzar or indice % cada == 0:
        _api_call(lambda: progreso_ws.update(
            [[indice, f'Siguiente a procesar: candidato #{indice+1}']],
            'A2:B2'
        ), etiqueta='guardar_progreso')


ABREVIATURAS_COMPUESTAS = [
    'elem. compos.', 'loc. verb.', 'loc. verbs.', 'loc. conjunt.', 'loc. adv.',
    'loc. prep.', 'art. deter.', 'pron. interrog.', 'pron. relat.', 'pron. dem.',
    'pron. person.', 'adj. poses.', 'adv. dem.', 'p. us.',
]
ABREVIATURAS_SIMPLES = [
    'adj.', 'adv.', 'conj.', 'desus.', 'deter.', 'interj.', 'f.', 'm.', 's.',
    'interrog.', 'poses.', 'pref.', 'suf.', 'prep.', 'propos.', 'onomat.',
    'sust.', 'pl.', 'may.', 'Ortogr.', 'malson.',
]

_vistas = set()
_ABREVIATURAS_UNICAS = []
for _a in ABREVIATURAS_COMPUESTAS + ABREVIATURAS_SIMPLES:
    if _a not in _vistas:
        _vistas.add(_a)
        _ABREVIATURAS_UNICAS.append(_a)
_ABREVIATURAS_UNICAS.sort(key=len, reverse=True)

_PATRON_ABREVIATURAS = re.compile(
    r'(?:^|[\s(\[,;])(' +
    '|'.join(re.escape(a) for a in _ABREVIATURAS_UNICAS) +
    r')(?=\s|$|[)\]:,;.])',
    re.MULTILINE
)


def extraer_abreviaturas(contenido):
    encontradas = []
    vistas = set()
    for m in _PATRON_ABREVIATURAS.finditer(contenido):
        tok = m.group(1)
        if tok not in vistas:
            vistas.add(tok)
            encontradas.append(tok)
    return ', '.join(encontradas)


def extraer_sinonimos_antonimos(contenido):
    m_sin = re.search(
        r'Sinónimos o afines de «[^»]+»\s*\n(.*?)'
        r'(?=\n\s*Antónimos u opuestos de «|\n\s*Artículo\s|\n\s*Palabra del día|\Z)',
        contenido, re.DOTALL
    )
    m_ant = re.search(
        r'Antónimos u opuestos de «[^»]+»\s*\n(.*?)'
        r'(?=\n\s*Palabra del día|\n\s*Artículo\s|\Z)',
        contenido, re.DOTALL
    )

    def limpiar(bloque):
        if not bloque:
            return ''
        texto = ' '.join(l.strip() for l in bloque.splitlines() if l.strip())
        items = [re.sub(r'\d+$', '', x.strip(' .')) for x in texto.split(',')]
        items = [x.strip() for x in items if x.strip()]
        vistos, resultado = set(), []
        for it in items:
            clave = it.lower()
            if clave not in vistos:
                vistos.add(clave)
                resultado.append(it)
        return ', '.join(resultado)

    return limpiar(m_sin.group(1) if m_sin else ''), limpiar(m_ant.group(1) if m_ant else '')


def construir_enlace(palabra):
    return f'https://dle.rae.es/{quote(palabra)}'


def verificar_en_dle(driver, palabra, medicion=None):
    enlace = construir_enlace(palabra)

    _t0 = time.time()
    driver.get(enlace)
    _t1 = time.time()
    pausa(1.5, 2.0)
    _t2 = time.time()
    scroll_aleatorio(driver)
    _t3 = time.time()

    if medicion is not None:
        medicion['dle']    = _t1 - _t0
        medicion['pausa']  = _t2 - _t1
        medicion['scroll'] = _t3 - _t2

    contenido    = driver.find_element('tag name', 'body').text
    enlace_final = driver.current_url

    en_dle = 'Artículo' in contenido
    abreviaturas = extraer_abreviaturas(contenido) if en_dle else ''
    sins, ants   = extraer_sinonimos_antonimos(contenido) if en_dle else ('', '')

    return en_dle, abreviaturas, sins, ants, enlace, enlace_final


# ============================================================
# 8. EJECUCIÓN POR BLOQUES (idéntico al notebook original)
#    Sin límite artificial de "sesión Colab": aquí el límite real es
#    el de 6h por job de GitHub Actions.
# ============================================================
BLOQUE             = 150
PAUSA_LARGA_CADA   = 25
BLOQUES_POR_SESION = 25
PAUSA_ENTRE_MIN    = 3
PAUSA_ENTRE_MAX    = 5
TAMANO_LOTE        = 25   # palabras acumuladas antes de escribir a Sheets en un solo append_rows

# ------------------------------------------------------------------
# MEDICIÓN TEMPORAL (TEMPORAL — remover o comentar cuando ya no se
# necesite diagnosticar tiempos; no afecta la lógica del script).
# ------------------------------------------------------------------
_MEDIR_TIEMPOS = True
_tiempos_globales = {'dle': 0.0, 'pausa': 0.0, 'scroll': 0.0, 'sheets': 0.0, 'otros': 0.0}


def _pausa_entre_bloques(n_bloque, total_bloques):
    minutos = random.uniform(PAUSA_ENTRE_MIN, PAUSA_ENTRE_MAX)
    segs    = int(minutos * 60)
    eta     = datetime.datetime.now() + datetime.timedelta(seconds=segs)
    print(f'\n── Pausa tras bloque {n_bloque}/{total_bloques}: '
          f'{minutos:.1f} min — reanuda ~{eta.strftime("%H:%M:%S")} ──')
    transcurrido = 0
    while transcurrido < segs:
        dormir = min(60, segs - transcurrido)
        time.sleep(dormir)
        transcurrido += dormir
        restantes = segs - transcurrido
        if restantes > 0:
            print(f'   ⏱  {restantes // 60}m {restantes % 60:02d}s...')
    print('   ▶ Reanudando.\n')


def _ejecutar_un_bloque():
    inicio = leer_progreso(progreso_ws)
    fin    = min(inicio + BLOQUE, len(candidatos))

    if inicio >= len(candidatos):
        return 0, False

    existentes = {fila[0] for fila in
                  _api_call(lambda: worksheet.get_all_values(), etiqueta='leer_existentes')[1:] if fila}
    print(f'► Candidatos {inicio+1}–{fin} de {len(candidatos)} '
          f'({len(candidatos) - fin} pendientes tras este bloque)')

    palabras_nuevas    = 0
    procesados_bloque  = 0
    lote               = []   # filas [palabra, enlace, abrevs, sins, ants] pendientes de escribir
    tiempos_bloque     = {'dle': 0.0, 'pausa': 0.0, 'scroll': 0.0, 'sheets': 0.0}
    n_palabras_medidas = 0

    def _flush_lote(indice_hasta):
        """Escribe el lote acumulado en UNA sola llamada (append_rows) y solo
        entonces avanza el checkpoint de progreso a indice_hasta. Así el
        progreso guardado siempre coincide con lo que realmente quedó escrito
        en la hoja, aunque el job se corte a mitad de un lote."""
        nonlocal lote
        if lote:
            _ts = time.time()
            _api_call(lambda: worksheet.append_rows(lote, value_input_option='RAW'),
                      etiqueta='flush_lote')
            _te = time.time()
            if _MEDIR_TIEMPOS:
                tiempos_bloque['sheets']    += (_te - _ts)
                _tiempos_globales['sheets'] += (_te - _ts)
                print(f'  [tiempo] flush de {len(lote)} filas a Sheets: {_te - _ts:.2f}s')
            lote = []
        guardar_progreso(progreso_ws, indice_hasta, forzar=True)

    for i in range(inicio, fin):
        palabra = candidatos[i]

        if palabra in existentes:
            print(f'[{i+1}] "{palabra}" ya existe — omitida.')
            procesados_bloque += 1
            continue

        medicion = {} if _MEDIR_TIEMPOS else None
        try:
            en_dle, abrevs, sins, ants, enlace, enlace_final = verificar_en_dle(
                driver, palabra, medicion=medicion)
        except Exception as e:
            print(f'[{i+1}] Error con "{palabra}": {e}')
            # Escribimos primero lo acumulado para no perderlo, y luego
            # marcamos este candidato como procesado (se omite en el futuro).
            _flush_lote(i + 1)
            procesados_bloque += 1
            continue

        lote.append([palabra, enlace, abrevs, sins, ants])
        existentes.add(palabra)
        palabras_nuevas += 1

        if _MEDIR_TIEMPOS and medicion:
            tiempos_bloque['dle']       += medicion.get('dle', 0.0)
            tiempos_bloque['pausa']     += medicion.get('pausa', 0.0)
            tiempos_bloque['scroll']    += medicion.get('scroll', 0.0)
            _tiempos_globales['dle']    += medicion.get('dle', 0.0)
            _tiempos_globales['pausa']  += medicion.get('pausa', 0.0)
            _tiempos_globales['scroll'] += medicion.get('scroll', 0.0)
            n_palabras_medidas += 1

        icono  = '✔' if en_dle else '✘'
        prev_a = abrevs[:40] + ('…' if len(abrevs) > 40 else '')
        prev_s = sins[:35]   + ('…' if len(sins) > 35   else '')
        sufijo_tiempo = ''
        if _MEDIR_TIEMPOS and medicion:
            sufijo_tiempo = (f'  (dle:{medicion.get("dle",0):.2f}s '
                              f'pausa:{medicion.get("pausa",0):.2f}s '
                              f'scroll:{medicion.get("scroll",0):.2f}s)')
        print(f'[{i+1}] {icono} {palabra}  |  {prev_a}  |  Sin: {prev_s}{sufijo_tiempo}')

        procesados_bloque += 1

        if len(lote) >= TAMANO_LOTE:
            _flush_lote(i + 1)

        if procesados_bloque % PAUSA_LARGA_CADA == 0:
            pausa_larga()

    # Remanente del lote al cierre del bloque (si el bloque no cerró justo
    # en un múltiplo de TAMANO_LOTE).
    _flush_lote(fin)

    hay_mas = fin < len(candidatos)
    print(f'Bloque terminado. Palabras nuevas: {palabras_nuevas}.')

    if _MEDIR_TIEMPOS and n_palabras_medidas:
        print(f'  [tiempo] Resumen bloque — '
              f'dle: {tiempos_bloque["dle"]:.1f}s (prom {tiempos_bloque["dle"]/n_palabras_medidas:.2f}s/palabra) | '
              f'pausa: {tiempos_bloque["pausa"]:.1f}s (prom {tiempos_bloque["pausa"]/n_palabras_medidas:.2f}s/palabra) | '
              f'scroll: {tiempos_bloque["scroll"]:.1f}s | '
              f'sheets (lotes): {tiempos_bloque["sheets"]:.1f}s '
              f'({tiempos_bloque["sheets"]/n_palabras_medidas:.3f}s/palabra equivalente)')

    return palabras_nuevas, hay_mas


# ============================================================
# 9. NAVEGADOR + LOOP PRINCIPAL
# ============================================================
driver = crear_driver()
print('Navegador listo.')

_SEP     = '═' * 52
t_inicio = datetime.datetime.now()
total    = 0

print(_SEP)
print(f'Archivo: {NOMBRE_ARCHIVO}  |  Hoja: {NOMBRE_HOJA}  |  Candidatos: {len(candidatos)}')
print(f'Sesión: {t_inicio.strftime("%Y-%m-%d %H:%M:%S")}')
print(f'Bloques planificados: {BLOQUES_POR_SESION}  (~{BLOQUES_POR_SESION * BLOQUE} palabras)')
print(f'Pausa entre bloques: {PAUSA_ENTRE_MIN}–{PAUSA_ENTRE_MAX} min')
print(f'Punto de inicio: candidato #{leer_progreso(progreso_ws)+1} de {len(candidatos)}')
print(_SEP + '\n')

try:
    for n in range(1, BLOQUES_POR_SESION + 1):
        if leer_progreso(progreso_ws) >= len(candidatos):
            print('✅ Lista completa procesada.')
            break

        print(f'══ BLOQUE {n}/{BLOQUES_POR_SESION} {"═" * 38}')

        try:
            p, hay_mas = _ejecutar_un_bloque()
        except Exception as e:
            print(f'\n⚠ Error inesperado en bloque {n}: {e}')
            print('El progreso está guardado. El siguiente run del workflow retomará desde aquí.')
            break

        total += p

        if not hay_mas:
            print('\n✅ Lista completa procesada.')
            break

        if n < BLOQUES_POR_SESION:
            _pausa_entre_bloques(n, BLOQUES_POR_SESION)
finally:
    driver.quit()

duracion = datetime.datetime.now() - t_inicio
actual   = leer_progreso(progreso_ws)
print(f'\n{_SEP}')
print(f'Sesión terminada. Duración: {str(duracion).split(".")[0]}')
print(f'Esta sesión — palabras nuevas: {total}')
print(f'Progreso acumulado: {actual}/{len(candidatos)} candidatos procesados')
if actual < len(candidatos):
    print(f'\nQuedan {len(candidatos) - actual} candidatos en este archivo.')
    print('El workflow puede volver a ejecutarse (manual o programado) y retomará automáticamente.')
else:
    print('\nEste archivo quedó completo.')

if _MEDIR_TIEMPOS and total > 0:
    print(f'\n{_SEP}')
    print('[tiempo] RESUMEN GLOBAL DE LA SESIÓN (medición temporal — temporal)')
    for _clave, _etiqueta in (('dle', 'Carga DLE'), ('pausa', 'Pausa deliberada'),
                               ('scroll', 'Scroll'), ('sheets', 'Escritura a Sheets (lotes)')):
        _seg = _tiempos_globales[_clave]
        print(f'  {_etiqueta:28s}: {_seg:8.1f}s total | {_seg/total:6.3f}s/palabra promedio')
    _suma = sum(_tiempos_globales.values())
    print(f'  {"Suma de componentes":28s}: {_suma:8.1f}s total | {_suma/total:6.3f}s/palabra promedio')
    print(f'  {"Duración real de sesión":28s}: {duracion.total_seconds():8.1f}s '
          f'({duracion.total_seconds()/total:6.3f}s/palabra efectivo)')
    _no_explicado = duracion.total_seconds() - _suma
    print(f'  {"No explicado (reintentos, pausas entre bloques, pausas largas, overhead)":74s}: '
          f'{_no_explicado:8.1f}s')
