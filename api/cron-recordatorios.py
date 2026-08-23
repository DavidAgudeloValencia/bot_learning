"""
Cron job diario: revisa el banco de preguntas de cada chat y, si llevan
varios días sin repasar algo que ya está vencido, manda un recordatorio
por Telegram. Vercel invoca este endpoint automáticamente según el
horario definido en vercel.json (plan Hobby: máximo 1 vez al día).
"""

import os
from datetime import datetime, timezone
import requests
from flask import Flask, request, jsonify
from supabase import create_client

app = Flask(__name__)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_KEY"]
CRON_SECRET = os.environ.get("CRON_SECRET")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

DIAS_MINIMOS_SIN_REPASAR = 3


def send_message(chat_id, text):
    requests.post(
        f"{TELEGRAM_API}/sendMessage",
        json={"chat_id": chat_id, "text": text},
        timeout=10,
    )


@app.route("/api/cron-recordatorios", methods=["GET", "POST"])
def cron_recordatorios():
    # Vercel manda automáticamente el CRON_SECRET como Bearer token.
    # Si alguien más pega esta URL sin el secreto correcto, se rechaza.
    auth_header = request.headers.get("Authorization", "")
    if CRON_SECRET and auth_header != f"Bearer {CRON_SECRET}":
        return jsonify({"error": "unauthorized"}), 401

    ahora = datetime.now(timezone.utc)

    # Solo nos interesan preguntas que ya están vencidas (listas para repasar)
    resp = (
        supabase.table("preguntas_generadas")
        .select("chat_id, criterio, ultima_vez, created_at")
        .lte("proximo_repaso", ahora.isoformat())
        .execute()
    )

    # Agrupamos por (chat_id, criterio) para mandar un solo mensaje por materia,
    # no uno por cada pregunta pendiente.
    grupos = {}
    for fila in resp.data:
        clave = (fila["chat_id"], fila["criterio"] or "tus apuntes")
        referencia = fila["ultima_vez"] or fila["created_at"]
        grupos.setdefault(clave, []).append(referencia)

    enviados = 0
    for (chat_id, criterio), referencias in grupos.items():
        referencias_dt = [
            datetime.fromisoformat(r.replace("Z", "+00:00")) for r in referencias if r
        ]
        if not referencias_dt:
            continue

        dias_sin_repasar = (ahora - min(referencias_dt)).days
        if dias_sin_repasar < DIAS_MINIMOS_SIN_REPASAR:
            continue

        pendientes = len(referencias)
        send_message(
            chat_id,
            f"⏰ Llevas {dias_sin_repasar} día(s) sin repasar {criterio}. "
            f"Tienes {pendientes} pregunta(s) esperando — manda /repasar {criterio} cuando puedas.",
        )
        enviados += 1

    return jsonify({"ok": True, "recordatorios_enviados": enviados})


# Para pruebas locales
if __name__ == "__main__":
    app.run(port=8001, debug=True)