import os
import base64
import json
from io import BytesIO
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape
import requests
from flask import Flask, request, jsonify
from supabase import create_client
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

GEMINI_MODEL = "gemini-3.1-flash-lite"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
)

TRANSCRIPTION_PROMPT = (
    "Transcribe el texto manuscrito de esta imagen de apuntes de clase "
    "(la persona es estudiante de enfermería). Reglas:\n"
    "- Devuelve SOLO el texto transcrito, sin comentarios ni explicaciones.\n"
    "- Corrige errores obvios de ortografía, pero conserva términos médicos "
    "tal cual, aunque no los reconozcas.\n"
    "- Si hay diagramas o dibujos, descríbelos brevemente entre corchetes, "
    "ej: [diagrama del corazón con flechas de flujo sanguíneo].\n"
    "- Si una palabra es realmente ilegible, escribe [ilegible] en vez de adivinar."
)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Escala de repaso espaciado: índice = nivel_dominio, valor = días hasta el próximo repaso.
# Acertar sube de nivel (repasas cada vez menos seguido); fallar te manda casi al inicio.
INTERVALOS_DIAS = [0, 1, 3, 7, 14, 30, 60]


@app.route("/api/webhook", methods=["POST"])
def webhook():
    update = request.get_json(silent=True) or {}

    if "callback_query" in update:
        handle_callback_query(update["callback_query"])
        return jsonify({"ok": True})

    message = update.get("message")

    if not message:
        # Puede ser otro tipo de update (edición, etc.) - lo ignoramos por ahora
        return jsonify({"ok": True})

    chat_id = message["chat"]["id"]

    if "photo" in message:
        handle_photo(chat_id, message)
    elif "text" in message:
        handle_text(chat_id, message["text"])
    else:
        send_message(chat_id, "Por ahora solo sé leer fotos y texto. 🙂")

    return jsonify({"ok": True})


def ask_gemini(prompt):
    """Manda un prompt de solo texto a Gemini y devuelve la respuesta."""
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    resp = requests.post(GEMINI_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def send_message(chat_id, text):
    # Telegram rechaza mensajes de más de 4096 caracteres; si el texto es
    # más largo lo partimos en varios mensajes.
    LIMITE = 4000
    for i in range(0, len(text), LIMITE):
        requests.post(
            f"{TELEGRAM_API}/sendMessage",
            json={"chat_id": chat_id, "text": text[i:i + LIMITE]},
            timeout=10,
        )


def send_message_con_botones(chat_id, text, botones):
    """botones: lista de (texto_boton, callback_data)."""
    keyboard = {"inline_keyboard": [[{"text": t, "callback_data": cd}] for t, cd in botones]}
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text, "reply_markup": keyboard},
        timeout=10,
    )


def responder_callback(callback_query_id, texto=None):
    """Le quita el 'reloj de carga' al botón que tocaron en Telegram."""
    payload = {"callback_query_id": callback_query_id}
    if texto:
        payload["text"] = texto
    requests.post(f"{TELEGRAM_API}/answerCallbackQuery", json=payload, timeout=10)


def send_document(chat_id, filename, file_bytes, caption=None):
    requests.post(
        f"{TELEGRAM_API}/sendDocument",
        data={"chat_id": chat_id, "caption": caption or ""},
        files={"document": (filename, file_bytes, "application/pdf")},
        timeout=30,
    )


def get_estado(chat_id):
    """Devuelve (materia_activa, tema_activo) para este chat, o (None, None)."""
    resp = (
        supabase.table("estado_chat")
        .select("materia_activa, tema_activo")
        .eq("chat_id", chat_id)
        .execute()
    )
    if resp.data:
        return resp.data[0]["materia_activa"], resp.data[0]["tema_activo"]
    return None, None


def set_materia_activa(chat_id, materia):
    # Al cambiar de materia, se resetea el tema activo para no arrastrar
    # un tema de otra materia por accidente.
    supabase.table("estado_chat").upsert(
        {"chat_id": chat_id, "materia_activa": materia, "tema_activo": None}
    ).execute()


def set_tema_activo(chat_id, tema):
    supabase.table("estado_chat").upsert(
        {"chat_id": chat_id, "tema_activo": tema}
    ).execute()


def handle_text(chat_id, text):
    text = text.strip()

    if text.startswith("/start"):
        send_message(
            chat_id,
            "¡Hola! 👋 Así me usas:\n\n"
            "1️⃣ /materia <nombre> — fija la materia activa (ej. /materia Farmacología)\n"
            "2️⃣ (opcional) /tema <nombre> — fija el tema dentro de la materia\n"
            "3️⃣ Mándame una foto de tus apuntes y la guardo transcrita bajo esa materia/tema\n\n"
            "Para estudiar:\n"
            "📋 /misapuntes — ve qué llevas subido\n"
            "📝 /resumen <materia> — resumen de estudio de esa materia\n"
            "❓ /quiz <materia o tema> — preguntas tipo examen con respuestas\n"
            "🔁 /repasar [materia] — repaso espaciado, prioriza lo que fallas\n"
            "🔍 /buscar <palabra> — busca esa palabra en tus apuntes\n"
            "📄 /exportar <materia> — te mando un PDF con todos tus apuntes de esa materia",
        )

    elif text.startswith("/materia"):
        nombre = text[len("/materia"):].strip()
        if not nombre:
            send_message(chat_id, "Usa: /materia Farmacología (con el nombre después del comando)")
            return
        set_materia_activa(chat_id, nombre)
        send_message(chat_id, f"✅ Materia activa: {nombre}\n(Las próximas fotos se guardan ahí)")

    elif text.startswith("/tema"):
        nombre = text[len("/tema"):].strip()
        if not nombre:
            send_message(chat_id, "Usa: /tema Antibióticos (con el nombre después del comando)")
            return
        materia_activa, _ = get_estado(chat_id)
        if not materia_activa:
            send_message(chat_id, "Primero fija una materia con /materia <nombre>")
            return
        set_tema_activo(chat_id, nombre)
        send_message(chat_id, f"✅ Tema activo: {nombre} (dentro de {materia_activa})")

    elif text.startswith("/misapuntes"):
        handle_misapuntes(chat_id)

    elif text.startswith("/resumen"):
        materia = text[len("/resumen"):].strip()
        if not materia:
            send_message(chat_id, "Usa: /resumen Farmacología")
            return
        handle_resumen(chat_id, materia)

    elif text.startswith("/quiz"):
        criterio = text[len("/quiz"):].strip()
        if not criterio:
            send_message(chat_id, "Usa: /quiz Farmacología (o un tema puntual, ej. /quiz Antibióticos)")
            return
        handle_quiz(chat_id, criterio)

    elif text.startswith("/buscar"):
        palabra = text[len("/buscar"):].strip()
        if not palabra:
            send_message(chat_id, 'Usa: /buscar "dosis paracetamol"')
            return
        handle_buscar(chat_id, palabra)

    elif text.startswith("/repasar"):
        criterio = text[len("/repasar"):].strip() or None
        handle_repasar(chat_id, criterio)

    elif text.startswith("/exportar"):
        materia = text[len("/exportar"):].strip()
        if not materia:
            send_message(chat_id, "Usa: /exportar Farmacología")
            return
        handle_exportar(chat_id, materia)

    else:
        send_message(
            chat_id,
            "No reconozco ese comando. Usa /materia, /tema, /misapuntes, "
            "o mándame directamente una foto de tus apuntes.",
        )


def handle_misapuntes(chat_id):
    resp = (
        supabase.table("apuntes")
        .select("materia")
        .eq("chat_id", chat_id)
        .execute()
    )

    if not resp.data:
        send_message(chat_id, "Todavía no tienes apuntes guardados. ¡Mándame una foto!")
        return

    conteo = {}
    for fila in resp.data:
        materia = fila["materia"] or "(sin materia asignada)"
        conteo[materia] = conteo.get(materia, 0) + 1

    lineas = [f"• {materia}: {cantidad}" for materia, cantidad in sorted(conteo.items())]
    total = sum(conteo.values())
    send_message(chat_id, f"📚 Tienes {total} apunte(s) guardados:\n\n" + "\n".join(lineas))


def _apuntes_con_texto(chat_id, materia=None, tema=None):
    """Trae los apuntes transcritos del chat, opcionalmente filtrados."""
    query = (
        supabase.table("apuntes")
        .select("tema, texto_transcrito, created_at")
        .eq("chat_id", chat_id)
        .not_.is_("texto_transcrito", "null")
    )
    if materia:
        query = query.ilike("materia", f"%{materia}%")
    if tema:
        query = query.ilike("tema", f"%{tema}%")

    resp = query.order("created_at").execute()
    return resp.data


def handle_resumen(chat_id, materia):
    apuntes = _apuntes_con_texto(chat_id, materia=materia)

    if not apuntes:
        send_message(chat_id, f"No encontré apuntes transcritos de '{materia}'. ¿Escribiste bien el nombre de la materia?")
        return

    send_message(chat_id, f"⏳ Armando el resumen de {materia} ({len(apuntes)} apunte(s))...")

    bloque = "\n\n---\n\n".join(
        f"[{a['tema'] or 'sin tema'}] {a['texto_transcrito']}" for a in apuntes
    )

    prompt = (
        f"Eres tutor de una estudiante de enfermería. A continuación tienes sus "
        f"apuntes de clase manuscritos y transcritos, de la materia '{materia}'. "
        f"Genera un resumen de estudio claro y organizado por sub-temas, en viñetas, "
        f"que le sirva para repasar antes de un examen. No inventes información que "
        f"no esté en los apuntes.\n\n{bloque}"
    )

    try:
        resumen = ask_gemini(prompt)
    except Exception as e:
        print(f"Error generando resumen: {e}")
        send_message(chat_id, "Se me cayó la conexión generando el resumen. Intenta de nuevo en un rato.")
        return

    send_message(chat_id, f"📝 Resumen de {materia}:\n\n{resumen}")


class SinApuntesError(Exception):
    """Se usa cuando no hay apuntes que coincidan con la materia/tema pedido."""
    pass


def generar_preguntas_ia(chat_id, criterio):
    """Busca apuntes que coincidan con materia o tema, y le pide a Gemini
    preguntas de opción múltiple. Lanza SinApuntesError si no hay apuntes,
    o cualquier otra excepción si Gemini falla. Devuelve la lista de preguntas."""
    apuntes_por_materia = _apuntes_con_texto(chat_id, materia=criterio)
    apuntes_por_tema = _apuntes_con_texto(chat_id, tema=criterio)

    vistos = set()
    apuntes = []
    for a in apuntes_por_materia + apuntes_por_tema:
        clave = (a["tema"], a["texto_transcrito"])
        if clave not in vistos:
            vistos.add(clave)
            apuntes.append(a)

    if not apuntes:
        raise SinApuntesError(f"No encontré apuntes transcritos relacionados con '{criterio}'.")

    bloque = "\n\n---\n\n".join(
        f"[{a['tema'] or 'sin tema'}] {a['texto_transcrito']}" for a in apuntes
    )

    prompt = (
        f"Eres profesor de enfermería preparando a una estudiante para su examen "
        f"de '{criterio}'. Con base ÚNICAMENTE en estos apuntes, genera entre 5 y 8 "
        f"preguntas de opción múltiple con 4 opciones (A, B, C, D), una sola correcta.\n\n"
        f"Responde ÚNICAMENTE con un JSON válido (sin \\`\\`\\`json ni texto extra), "
        f"con este formato exacto:\n"
        f'[{{"pregunta": "...", "opciones": {{"A": "...", "B": "...", "C": "...", "D": "..."}}, '
        f'"correcta": "B", "explicacion": "..."}}]\n\n'
        f"No inventes datos que no estén en los apuntes.\n\n{bloque}"
    )

    respuesta = ask_gemini(prompt)
    preguntas = parse_json_gemini(respuesta)
    if not preguntas:
        raise ValueError("Gemini devolvió una lista vacía")
    return preguntas


def guardar_banco_preguntas(chat_id, criterio, preguntas):
    """Guarda las preguntas generadas en el banco permanente para /repasar."""
    filas = [
        {
            "chat_id": chat_id,
            "criterio": criterio,
            "pregunta": p["pregunta"],
            "opciones": p["opciones"],
            "correcta": p["correcta"],
            "explicacion": p.get("explicacion"),
            "proximo_repaso": datetime.now(timezone.utc).isoformat(),
        }
        for p in preguntas
    ]
    supabase.table("preguntas_generadas").insert(filas).execute()


def handle_quiz(chat_id, criterio):
    send_message(chat_id, f"⏳ Generando quiz de {criterio}...")

    try:
        preguntas = generar_preguntas_ia(chat_id, criterio)
    except SinApuntesError as e:
        send_message(chat_id, str(e))
        return
    except Exception as e:
        print(f"Error generando quiz: {e}")
        send_message(chat_id, "Se me cayó la conexión generando el quiz. Intenta de nuevo en un rato.")
        return

    supabase.table("quiz_activo").upsert(
        {
            "chat_id": chat_id,
            "criterio": criterio,
            "preguntas": preguntas,
            "indice": 0,
            "aciertos": 0,
        }
    ).execute()

    # También quedan guardadas para /repasar más adelante
    try:
        guardar_banco_preguntas(chat_id, criterio, preguntas)
    except Exception as e:
        print(f"Error guardando banco de preguntas: {e}")

    send_message(chat_id, f"❓ Quiz de {criterio} — {len(preguntas)} pregunta(s). ¡Vamos!")
    enviar_pregunta_actual(chat_id)


def _elegir_pregunta_pendiente(chat_id, criterio=None):
    """La pregunta vencida hace más tiempo (o nunca vista), o None si no hay ninguna vencida."""
    ahora = datetime.now(timezone.utc).isoformat()
    query = (
        supabase.table("preguntas_generadas")
        .select("*")
        .eq("chat_id", chat_id)
        .lte("proximo_repaso", ahora)
    )
    if criterio:
        query = query.ilike("criterio", f"%{criterio}%")

    resp = query.order("proximo_repaso").limit(1).execute()
    return resp.data[0] if resp.data else None


def _proximo_vencimiento(chat_id, criterio=None):
    """Cuándo vence la próxima pregunta del banco (aunque todavía no toque), o None si el banco está vacío."""
    query = (
        supabase.table("preguntas_generadas")
        .select("proximo_repaso")
        .eq("chat_id", chat_id)
    )
    if criterio:
        query = query.ilike("criterio", f"%{criterio}%")

    resp = query.order("proximo_repaso").limit(1).execute()
    return resp.data[0]["proximo_repaso"] if resp.data else None


def handle_repasar(chat_id, criterio):
    pregunta = _elegir_pregunta_pendiente(chat_id, criterio)

    if pregunta:
        enviar_pregunta_repaso(chat_id, pregunta)
        return

    proximo = _proximo_vencimiento(chat_id, criterio)

    if proximo:
        # Ya hay banco de preguntas para este criterio, solo que ninguna vence todavía
        fecha = datetime.fromisoformat(proximo.replace("Z", "+00:00")).strftime("%d de %B")
        send_message(
            chat_id,
            f"🎉 No tienes repasos pendientes {f'de {criterio}' if criterio else ''} por ahora.\n"
            f"Tu próxima pregunta vence el {fecha}.",
        )
        return

    # No hay banco todavía para este criterio: hay que generarlo primero
    if not criterio:
        send_message(
            chat_id,
            "Todavía no tienes ninguna pregunta guardada para repasar. "
            "Usa /repasar <materia> para generar tu primer banco de preguntas "
            "(o corre /quiz primero, eso también las guarda).",
        )
        return

    send_message(chat_id, f"⏳ No tenías preguntas de {criterio} guardadas, armando tu primer banco...")

    try:
        preguntas = generar_preguntas_ia(chat_id, criterio)
        guardar_banco_preguntas(chat_id, criterio, preguntas)
    except SinApuntesError as e:
        send_message(chat_id, str(e))
        return
    except Exception as e:
        print(f"Error generando banco de repaso: {e}")
        send_message(chat_id, "Se me cayó la conexión generando las preguntas. Intenta de nuevo en un rato.")
        return

    pregunta = _elegir_pregunta_pendiente(chat_id, criterio)
    enviar_pregunta_repaso(chat_id, pregunta)


def enviar_pregunta_repaso(chat_id, pregunta):
    etiqueta = pregunta["criterio"] or "general"
    texto = f"🔁 Repaso ({etiqueta}):\n\n{pregunta['pregunta']}"
    botones = [
        (f"{letra}) {opcion}", f"repasar:{pregunta['id']}:{letra}")
        for letra, opcion in pregunta["opciones"].items()
    ]
    send_message_con_botones(chat_id, texto, botones)


def parse_json_gemini(texto):
    """Gemini a veces envuelve el JSON en \\`\\`\\`json ... \\`\\`\\`; lo limpiamos antes de parsear."""
    limpio = texto.strip()
    if limpio.startswith("```"):
        limpio = limpio.split("```")[1]
        if limpio.startswith("json"):
            limpio = limpio[4:]
    return json.loads(limpio.strip())


def enviar_pregunta_actual(chat_id):
    resp = supabase.table("quiz_activo").select("*").eq("chat_id", chat_id).execute()
    if not resp.data:
        return

    quiz = resp.data[0]
    preguntas = quiz["preguntas"]
    indice = quiz["indice"]

    if indice >= len(preguntas):
        aciertos = quiz["aciertos"]
        total = len(preguntas)
        send_message(
            chat_id,
            f"🏁 ¡Quiz terminado! Aciertos: {aciertos}/{total}\n\n"
            "Manda /quiz otra vez (misma materia o tema) para repetirlo, "
            "o /quiz con otra materia para seguir estudiando.",
        )
        supabase.table("quiz_activo").delete().eq("chat_id", chat_id).execute()
        return

    p = preguntas[indice]
    texto = f"Pregunta {indice + 1}/{len(preguntas)}:\n\n{p['pregunta']}"
    botones = [
        (f"{letra}) {opcion}", f"quiz:{letra}")
        for letra, opcion in p["opciones"].items()
    ]
    send_message_con_botones(chat_id, texto, botones)


def handle_callback_query(callback_query):
    callback_query_id = callback_query["id"]
    chat_id = callback_query["message"]["chat"]["id"]
    data = callback_query.get("data", "")

    if data.startswith("quiz:"):
        _handle_callback_quiz(callback_query_id, chat_id, data)
    elif data.startswith("repasar:"):
        _handle_callback_repasar(callback_query_id, chat_id, data)
    else:
        responder_callback(callback_query_id)


def _handle_callback_quiz(callback_query_id, chat_id, data):
    letra_elegida = data.split(":", 1)[1]

    resp = supabase.table("quiz_activo").select("*").eq("chat_id", chat_id).execute()
    if not resp.data:
        responder_callback(callback_query_id, "Este quiz ya no está activo.")
        return

    quiz = resp.data[0]
    preguntas = quiz["preguntas"]
    indice = quiz["indice"]

    if indice >= len(preguntas):
        responder_callback(callback_query_id, "Este quiz ya terminó.")
        return

    p = preguntas[indice]
    correcta = p["correcta"]
    aciertos = quiz["aciertos"]

    if letra_elegida == correcta:
        responder_callback(callback_query_id, "¡Correcto! ✅")
        aciertos += 1
        feedback = f"✅ ¡Correcto! {p['opciones'][correcta]}"
    else:
        responder_callback(callback_query_id, "No era esa ❌")
        feedback = (
            f"❌ No era. La correcta era {correcta}) {p['opciones'][correcta]}"
        )

    if p.get("explicacion"):
        feedback += f"\n\n💡 {p['explicacion']}"

    send_message(chat_id, feedback)

    supabase.table("quiz_activo").update(
        {"indice": indice + 1, "aciertos": aciertos}
    ).eq("chat_id", chat_id).execute()

    enviar_pregunta_actual(chat_id)


def _handle_callback_repasar(callback_query_id, chat_id, data):
    _, pregunta_id, letra_elegida = data.split(":")
    pregunta_id = int(pregunta_id)

    resp = (
        supabase.table("preguntas_generadas")
        .select("*")
        .eq("id", pregunta_id)
        .execute()
    )
    if not resp.data:
        responder_callback(callback_query_id, "Esta pregunta ya no existe.")
        return

    p = resp.data[0]
    correcta = p["correcta"]
    acerto = letra_elegida == correcta

    nivel = p["nivel_dominio"]
    if acerto:
        responder_callback(callback_query_id, "¡Correcto! ✅")
        nivel = min(nivel + 1, len(INTERVALOS_DIAS) - 1)
        feedback = f"✅ ¡Correcto! {p['opciones'][correcta]}"
    else:
        responder_callback(callback_query_id, "No era esa ❌")
        nivel = max(nivel - 2, 0)
        feedback = f"❌ No era. La correcta era {correcta}) {p['opciones'][correcta]}"

    if p.get("explicacion"):
        feedback += f"\n\n💡 {p['explicacion']}"

    dias = INTERVALOS_DIAS[nivel]
    proximo_repaso = datetime.now(timezone.utc) + timedelta(days=dias)

    if dias == 0:
        feedback += "\n\n🔁 Te la vuelvo a preguntar pronto (todavía no la dominas)."
    else:
        feedback += f"\n\n🔁 Te la vuelvo a preguntar en {dias} día(s)."

    send_message(chat_id, feedback)

    supabase.table("preguntas_generadas").update(
        {
            "veces_repasada": p["veces_repasada"] + 1,
            "aciertos": p["aciertos"] + (1 if acerto else 0),
            "nivel_dominio": nivel,
            "ultima_vez": datetime.now(timezone.utc).isoformat(),
            "proximo_repaso": proximo_repaso.isoformat(),
        }
    ).eq("id", pregunta_id).execute()


def handle_buscar(chat_id, palabra):
    resp = (
        supabase.table("apuntes")
        .select("materia, tema, texto_transcrito, created_at")
        .eq("chat_id", chat_id)
        .ilike("texto_transcrito", f"%{palabra}%")
        .order("created_at", desc=True)
        .execute()
    )

    if not resp.data:
        send_message(chat_id, f"No encontré '{palabra}' en tus apuntes.")
        return

    lineas = [f"🔎 Encontré '{palabra}' en {len(resp.data)} apunte(s):\n"]
    for a in resp.data[:10]:
        materia = a["materia"] or "sin materia"
        tema = a["tema"] or "sin tema"
        fecha = (a["created_at"] or "")[:10]
        texto = a["texto_transcrito"] or ""

        idx = texto.lower().find(palabra.lower())
        inicio = max(0, idx - 60)
        fin = min(len(texto), idx + len(palabra) + 60)
        snippet = ("…" if inicio > 0 else "") + texto[inicio:fin] + ("…" if fin < len(texto) else "")

        lineas.append(f"\n📌 {materia} > {tema} ({fecha})\n{snippet}")

    if len(resp.data) > 10:
        lineas.append(f"\n\n(mostrando 10 de {len(resp.data)} resultados)")

    send_message(chat_id, "\n".join(lineas))


def generar_pdf_apuntes(materia, apuntes):
    """apuntes: lista de dicts con tema, texto_transcrito, created_at (ordenados por tema)."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=letter,
        topMargin=0.75 * inch, bottomMargin=0.75 * inch,
        leftMargin=0.75 * inch, rightMargin=0.75 * inch,
    )
    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph(f"Apuntes de {escape(materia)}", styles["Title"]))
    fecha_generacion = datetime.now(timezone.utc).strftime("Generado el %d/%m/%Y")
    story.append(Paragraph(fecha_generacion, styles["Normal"]))
    story.append(Spacer(1, 20))

    tema_actual = object()  # valor imposible, para forzar el primer encabezado
    for a in apuntes:
        tema = a["tema"] or "Sin tema"
        if tema != tema_actual:
            story.append(Spacer(1, 14))
            story.append(Paragraph(escape(tema), styles["Heading2"]))
            tema_actual = tema

        fecha = (a["created_at"] or "")[:10]
        story.append(Paragraph(f"<i>{escape(fecha)}</i>", styles["Normal"]))

        texto = escape(a["texto_transcrito"] or "").replace("\n", "<br/>")
        story.append(Paragraph(texto, styles["Normal"]))
        story.append(Spacer(1, 10))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()


def handle_exportar(chat_id, materia):
    resp = (
        supabase.table("apuntes")
        .select("tema, texto_transcrito, created_at")
        .eq("chat_id", chat_id)
        .ilike("materia", f"%{materia}%")
        .not_.is_("texto_transcrito", "null")
        .execute()
    )

    if not resp.data:
        send_message(chat_id, f"No encontré apuntes transcritos de '{materia}' para exportar.")
        return

    apuntes_ordenados = sorted(
        resp.data, key=lambda a: (a["tema"] or "", a["created_at"] or "")
    )

    send_message(chat_id, f"⏳ Armando el PDF de {materia} ({len(apuntes_ordenados)} apunte(s))...")

    try:
        pdf_bytes = generar_pdf_apuntes(materia, apuntes_ordenados)
    except Exception as e:
        print(f"Error generando PDF: {e}")
        send_message(chat_id, "Se me cayó armando el PDF. Intenta de nuevo en un rato.")
        return

    nombre_archivo = f"apuntes_{materia.strip().replace(' ', '_')}.pdf"
    send_document(chat_id, nombre_archivo, pdf_bytes, caption=f"📄 Tus apuntes de {materia}")


def transcribe_with_gemini(image_bytes):
    """Manda la imagen a Gemini y devuelve el texto transcrito (o None si falla)."""
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": TRANSCRIPTION_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": "image/jpeg",
                            "data": image_b64,
                        }
                    },
                ]
            }
        ]
    }

    resp = requests.post(GEMINI_URL, json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    # Estructura: candidates[0].content.parts[0].text
    return data["candidates"][0]["content"]["parts"][0]["text"].strip()


def handle_photo(chat_id, message):
    # Telegram manda la misma foto en varias resoluciones; la última es la más grande
    file_id = message["photo"][-1]["file_id"]

    # 1. Pedirle a Telegram la ruta del archivo
    file_info = requests.get(
        f"{TELEGRAM_API}/getFile", params={"file_id": file_id}, timeout=10
    ).json()
    file_path = file_info["result"]["file_path"]
    file_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file_path}"

    # 2. Descargar la imagen
    image_bytes = requests.get(file_url, timeout=15).content

    # 3. Subirla al bucket "apuntes" en Supabase Storage
    filename = f"{chat_id}_{message['message_id']}.jpg"
    supabase.storage.from_("apuntes").upload(
        filename, image_bytes, {"content-type": "image/jpeg"}
    )
    public_url = supabase.storage.from_("apuntes").get_public_url(filename)

    # 4. Transcribir con Gemini (si falla, seguimos guardando la imagen igual)
    try:
        texto_transcrito = transcribe_with_gemini(image_bytes)
    except Exception as e:
        print(f"Error transcribiendo con Gemini: {e}")
        texto_transcrito = None

    # 5. Etiquetar con la materia/tema activo del chat (si hay alguno fijado)
    materia_activa, tema_activo = get_estado(chat_id)

    # 6. Guardar el registro en la tabla "apuntes"
    supabase.table("apuntes").insert(
        {
            "chat_id": chat_id,
            "imagen_url": public_url,
            "texto_transcrito": texto_transcrito,
            "materia": materia_activa,
            "tema": tema_activo,
        }
    ).execute()

    ubicacion = materia_activa or "sin materia"
    if tema_activo:
        ubicacion += f" > {tema_activo}"

    if texto_transcrito:
        preview = texto_transcrito[:500]
        if len(texto_transcrito) > 500:
            preview += "…"
        send_message(
            chat_id,
            f"📸 ¡Guardado en {ubicacion}!\n\n{preview}",
        )
    else:
        send_message(
            chat_id,
            f"📸 Guardé tu apunte en {ubicacion}, pero la transcripción falló esta vez. "
            "Puedes reintentar mandando la foto de nuevo.",
        )

    if not materia_activa:
        send_message(
            chat_id,
            "💡 Tip: usa /materia <nombre> antes de mandar fotos para que "
            "queden organizadas por materia.",
        )


# Para pruebas locales: python api/webhook.py
if __name__ == "__main__":
    app.run(port=8000, debug=True)