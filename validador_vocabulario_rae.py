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
# 5. LOCALIZAR EL .TXT — vía API, explícito por nombre o ID
#    (en Actions no hay "carpeta propia por cuenta"; el archivo llega
#    como parámetro de la matriz del workflow)
# ============================================================
if args.archivo_id:
    _meta = drive_service.files().get(fileId=args.archivo_id, fields='id, name').execute()
    _ARCHIVO_TXT_ID = _meta['id']
    NOMBRE_ARCHIVO  = _meta['name']
else:
    if not CARPETA_ID:
        raise SystemExit('Falta CARPETA_ID en el entorno (requerido al usar --archivo por nombre)')
    _resp_txt = drive_service.files().list(
        q=(
            f"'{CARPETA_ID}' in parents and name='{args.archivo}' "
            "and trashed=false"
        ),
        fields='files(id, name)',
    ).execute()
    _archivos = _resp_txt.get('files', [])
    if not _archivos:
        raise FileNotFoundError(f'No se encontró "{args.archivo}" en la carpeta {CARPETA_ID}')
    _ARCHIVO_TXT_ID = _archivos[0]['id']
    NOMBRE_ARCHIVO  = _archivos[0]['name']

_num_match  = re.search(rf'{re.escape(PREFIJO_TXT)}(\w+)\.txt', NOMBRE_ARCHIVO)
_num        = _num_match.group(1) if _num_match else NOMBRE_ARCHIVO
NOMBRE_HOJA = f'{PREFIJO_HOJA}{_num}'

# Averiguar la carpeta contenedora real del archivo (para crear/ubicar la hoja ahí)
_meta_parents = drive_service.files().get(fileId=_ARCHIVO_TXT_ID, fields='parents').execute()
CARPETA_ID = (_meta_parents.get('parents') or [CARPETA_ID])[0]

# Descargar el .txt a un archivo local temporal
RUTA_TXT = f'/tmp/{NOMBRE_ARCHIVO}'
_request = drive_service.files().get_media(fileId=_ARCHIVO_TXT_ID)
with io.FileIO(RUTA_TXT, 'wb') as _fh:
    _downloader = MediaIoBaseDownload(_fh, _request)
    _done = False
    while not _done:
        _, _done = _downloader.next_chunk()

print(f'  Archivo asignado : {NOMBRE_ARCHIVO}')
print(f'  Hoja de cálculo  : {NOMBRE_HOJA}')


# ============================================================
# 6. HOJA DE CÁLCULO Y PESTAÑA DE PROGRESO (idéntico al notebook original)
# ============================================================
_resp_hojas = drive_service.files().list(
    q=(
        f"'{CARPETA_ID}' in parents and name='{NOMBRE_HOJA}' "
        "and mimeType='application/vnd.google-apps.spreadsheet' "
        "and trashed=false"
    ),
    fields='files(id, name)',
).execute()
_hojas_existentes = _resp_hojas.get('files', [])

if _hojas_existentes:
    sh = gc.open_by_key(_hojas_existentes[0]['id'])
    print(f'✔ Hoja existente reutilizada: {NOMBRE_HOJA}')
else:
    sh = gc.create(NOMBRE_HOJA)
    _file_id = sh.id
    _meta = drive_service.files().get(fileId=_file_id, fields='parents').execute()
    _padres_actuales = ','.join(_meta.get('parents', []))
    drive_service.files().update(
        fileId=_file_id,
        addParents=CARPETA_ID,
        removeParents=_padres_actuales,
        fields='id, parents',
    ).execute()
    print(f'✔ Hoja creada y movida a la carpeta del proyecto: {NOMBRE_HOJA}')

worksheet = sh.sheet1
valores = worksheet.get_all_values()
hoja_vacia = (not valores) or all(not any(fila) for fila in valores)
if hoja_vacia:
    worksheet.append_row(['Palabra', 'Enlace', 'Abreviaturas', 'Sinónimos', 'Antónimos'])
    print('Encabezados creados (5 columnas).')
else:
    print('La hoja ya tenía contenido; no se sobrescriben encabezados.')

try:
    progreso_ws = sh.worksheet('Progreso')
except gspread.WorksheetNotFound:
    progreso_ws = sh.add_worksheet(title='Progreso', rows=5, cols=2)
    progreso_ws.append_row(['ultimo_indice', 'descripcion'])
    progreso_ws.append_row([0, 'Inicio'])

print('Conectado a la hoja:', sh.title)


# ============================================================
# 7. CANDIDATOS + FUNCIONES DE EXTRACCIÓN (idéntico al notebook original)
# ============================================================
with open(RUTA_TXT, encoding='utf-8') as f:
    candidatos = [l.strip() for l in f if l.strip()]

print(f'Total de candidatos cargados desde {NOMBRE_ARCHIVO}: {len(candidatos)}')
print('Primeros 5:', candidatos[:5])
print('Últimos 5: ', candidatos[-5:])


def _sheets_write(fn, max_reintentos=4):
    espera = 60
    for intento in range(max_reintentos + 1):
        try:
            return fn()
        except Exception as e:
            if '429' in str(e) and intento < max_reintentos:
                print(f'  [429] Cuota de Sheets alcanzada. '
                      f'Esperando {espera}s (intento {intento+1}/{max_reintentos})...')
                time.sleep(espera)
                espera *= 2
            else:
                raise


def leer_progreso(progreso_ws):
    vals = progreso_ws.get_all_values()
    if len(vals) < 2 or not vals[1][0]:
        return 0
    return int(vals[1][0])


def guardar_progreso(progreso_ws, indice, forzar=False, cada=10):
    if forzar or indice % cada == 0:
        _sheets_write(lambda: progreso_ws.update(
            [[indice, f'Siguiente a procesar: candidato #{indice+1}']],
            'A2:B2'
        ))


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


def verificar_en_dle(driver, palabra):
    enlace = construir_enlace(palabra)
    driver.get(enlace)
    pausa(1.5, 2.0)
    scroll_aleatorio(driver)

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

    existentes = {fila[0] for fila in worksheet.get_all_values()[1:] if fila}
    print(f'► Candidatos {inicio+1}–{fin} de {len(candidatos)} '
          f'({len(candidatos) - fin} pendientes tras este bloque)')

    palabras_nuevas   = 0
    procesados_bloque = 0

    for i in range(inicio, fin):
        palabra = candidatos[i]

        if palabra in existentes:
            print(f'[{i+1}] "{palabra}" ya existe — omitida.')
            guardar_progreso(progreso_ws, i + 1, forzar=False)
            procesados_bloque += 1
            continue

        try:
            en_dle, abrevs, sins, ants, enlace, enlace_final = verificar_en_dle(driver, palabra)
        except Exception as e:
            print(f'[{i+1}] Error con "{palabra}": {e}')
            guardar_progreso(progreso_ws, i + 1, forzar=True)
            procesados_bloque += 1
            continue

        _sheets_write(lambda p=palabra, l=enlace, a=abrevs, s=sins, n=ants:
                      worksheet.append_row([p, l, a, s, n]))
        existentes.add(palabra)
        palabras_nuevas += 1

        icono  = '✔' if en_dle else '✘'
        prev_a = abrevs[:40] + ('…' if len(abrevs) > 40 else '')
        prev_s = sins[:35]   + ('…' if len(sins) > 35   else '')
        print(f'[{i+1}] {icono} {palabra}  |  {prev_a}  |  Sin: {prev_s}')

        guardar_progreso(progreso_ws, i + 1, forzar=False)
        procesados_bloque += 1

        if procesados_bloque % PAUSA_LARGA_CADA == 0:
            pausa_larga()

    guardar_progreso(progreso_ws, fin, forzar=True)
    hay_mas = fin < len(candidatos)
    print(f'Bloque terminado. Palabras nuevas: {palabras_nuevas}.')
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
