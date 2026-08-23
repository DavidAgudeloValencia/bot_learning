# 📚 Bot Learning — Asistente de Estudio y Repaso Espaciado con IA

**Bot Learning** es un bot de Telegram serverless diseñado para ser tu compañero de estudio inteligente. Permite capturar fotos de apuntes manuscritos, transcribirlos con IA (Google Gemini), organizarlos por materias y temas, generar resúmenes, realizar quizzes interactivos con botones, aplicar algoritmos de repaso espaciado (*Spaced Repetition*), buscar contenidos y exportar cuadernos completos a PDF.

---

## ✨ Características Principales

- 📸 **Captura y Almacenamiento en la Nube:** Sube fotos de tus apuntes directo desde Telegram a **Supabase Storage**.
- 🧠 **Transcripción Manuscrita con IA:** Emplea **Google Gemini 3.1 Flash Lite** para transcribir letra manuscrita, respetando terminología técnica (p. ej. términos médicos/enfermería) y describiendo diagramas.
- 🗂️ **Organización por Materia y Tema:** Define materias y subtemas activos para etiquetar tus apuntes de forma estructurada.
- 📝 **Resúmenes Automáticos:** Genera síntesis de estudio claras organizadas por temas a partir del contenido de tus notas (`/resumen`).
- ❓ **Quizzes Interactivos:** Genera cuestionarios de opción múltiple interactivos con botones *inline* en Telegram y retroalimentación explicativa inmediata (`/quiz`).
- 🔁 **Repaso Espaciado Adaptativo (*Spaced Repetition*):** Banco de preguntas inteligente que ajusta la frecuencia de repaso según tus aciertos y errores (`/repasar`).
- 🔍 **Búsqueda Inteligente:** Encuentra fragmentos y conceptos clave dentro de todos tus apuntes transcritos (`/buscar`).
- 📄 **Exportación a PDF:** Compila y maqueta tus apuntes ordenados por temas en un archivo PDF listo para imprimir o compartir (`/exportar`).
- ⏰ **Recordatorios Automáticos:** Cron job diario desplegado en Vercel que envía notificaciones cuando tienes temas vencidos por repasar.

---

## 🛠️ Stack Tecnológico

- **Lenguaje / Backend:** Python 3.9+ (Flask serverless)
- **IA / OCR:** Google Gemini API (`gemini-3.1-flash-lite`) vía Google AI Studio
- **Base de Datos y Almacenamiento:** Supabase (PostgreSQL + Supabase Storage)
- **Generación de Documentos:** ReportLab (PDF)
- **Despliegue & Crons:** Vercel Serverless Functions + Vercel Cron Jobs
- **Mensajería:** Telegram Bot API (Webhooks + Inline Keyboards)

---

## 📂 Estructura del Proyecto

```text
├── api/
│   ├── webhook.py              # Handler principal del bot de Telegram (Flask)
│   └── cron-recordatorios.py   # Endpoint diario de recordatorios (Vercel Cron)
├── .env.example                # Plantilla de variables de entorno
├── .gitignore                  # Reglas de exclusión para Git
├── requirements.txt            # Dependencias de Python
├── vercel.json                 # Configuración de Serverless Functions y Crons
└── README.md                   # Documentación del proyecto
```

---

## 🚀 Guía de Configuración y Despliegue

### 1. Crear el Bot en Telegram
1. Abre Telegram y conversa con [@BotFather](https://t.me/BotFather).
2. Ejecuta `/newbot` y sigue los pasos para asignar nombre y username al bot.
3. Copia el **Token HTTP API** generado (será tu `TELEGRAM_BOT_TOKEN`).

---

### 2. Configurar Supabase
1. Crea una cuenta y un proyecto nuevo en [Supabase](https://supabase.com).
2. En **Project Settings → API**, obtén:
   - **Project URL** (`SUPABASE_URL`)
   - **`service_role` Secret Key** (`SUPABASE_KEY`) *(Usa la `service_role` para permitir lecturas y escrituras desde las Serverless Functions).*
3. En **Storage**, crea un bucket llamado:
   - **Nombre:** `apuntes`
   - **Visibilidad:** Marcar como **Public bucket** (o privado si configuras URLs firmadas).
4. En **SQL Editor**, ejecuta el siguiente script para crear el esquema de base de datos:

```sql
-- 1. Tabla de apuntes y fotos
create table if not exists apuntes (
  id bigint generated always as identity primary key,
  chat_id bigint not null,
  imagen_url text not null,
  texto_transcrito text,
  materia text,
  tema text,
  created_at timestamp with time zone default now()
);

-- 2. Estado de navegación/contexto por chat
create table if not exists estado_chat (
  chat_id bigint primary key,
  materia_activa text,
  tema_activo text
);

-- 3. Estado de quizzes activos
create table if not exists quiz_activo (
  chat_id bigint primary key,
  criterio text,
  preguntas jsonb,
  indice int default 0,
  aciertos int default 0
);

-- 4. Banco de preguntas para repaso espaciado
create table if not exists preguntas_generadas (
  id bigint generated always as identity primary key,
  chat_id bigint not null,
  criterio text,
  pregunta text not null,
  opciones jsonb not null,
  correcta text not null,
  explicacion text,
  veces_repasada int default 0,
  aciertos int default 0,
  nivel_dominio int default 0,
  ultima_vez timestamp with time zone,
  proximo_repaso timestamp with time zone default now(),
  created_at timestamp with time zone default now()
);
```

---

### 3. Obtener API Key de Google Gemini
1. Ingresa a [Google AI Studio](https://aistudio.google.com).
2. Haz clic en **Get API key** → **Create API key**.
3. Copia la clave generada (será tu `GEMINI_API_KEY`).

---

### 4. Despliegue en Vercel

1. Instala el CLI de Vercel (si no lo tienes):
   ```bash
   npm install -g vercel
   ```
2. Inicia sesión y vincula el proyecto:
   ```bash
   vercel login
   vercel
   ```
3. Configura las variables de entorno en Vercel:
   ```bash
   vercel env add TELEGRAM_BOT_TOKEN
   vercel env add SUPABASE_URL
   vercel env add SUPABASE_KEY
   vercel env add GEMINI_API_KEY
   vercel env add CRON_SECRET
   ```
   *(Pega los valores correspondientes seleccionando el entorno **Production**).*

4. Despliega a producción:
   ```bash
   vercel --prod
   ```
   Al finalizar, obtendrás tu URL de producción (ej. `https://tu-proyecto.vercel.app`).

---

### 5. Configurar el Webhook de Telegram

Conecta tu bot de Telegram con el endpoint `/api/webhook` en Vercel ejecutando:

```bash
curl "https://api.telegram.org/bot<TU_TELEGRAM_BOT_TOKEN>/setWebhook?url=https://<TU_PROYECTO_VERCEL>.vercel.app/api/webhook"
```

Deberías recibir como respuesta:
```json
{"ok": true, "result": true, "description": "Webhook was set"}
```

---

## 💬 Comandos Disponibles en Telegram

| Comando | Descripción |
| :--- | :--- |
| `/start` | Muestra el menú de bienvenida y la lista de comandos disponibles. |
| `/materia <nombre>` | Establece la materia activa para las siguientes fotos y consultas. |
| `/tema <nombre>` | Establece un sub-tema dentro de la materia activa. |
| `/misapuntes` | Muestra un resumen del total de apuntes subidos por materia. |
| `/resumen <materia>` | Genera un resumen de estudio estructurado con IA de dicha materia. |
| `/quiz <materia o tema>` | Inicia una evaluación interactiva de opción múltiple basada en tus notas. |
| `/repasar [materia]` | Abre una sesión de preguntas según el algoritmo de repaso espaciado. |
| `/buscar <término>` | Busca palabras o conceptos específicos en las transcripciones. |
| `/exportar <materia>` | Genera y envía un documento PDF consolidado con los apuntes de esa materia. |

---

## 🔒 Variables de Entorno

| Variable | Descripción |
| :--- | :--- |
| `TELEGRAM_BOT_TOKEN` | Token de autenticación del bot de Telegram entregado por @BotFather. |
| `SUPABASE_URL` | URL de la API del proyecto en Supabase. |
| `SUPABASE_KEY` | Llave `service_role` de Supabase para acceso administrativo backend. |
| `GEMINI_API_KEY` | Clave de acceso a la API de Google Gemini (Google AI Studio). |
| `CRON_SECRET` | *(Opcional)* Token secreto enviado por Vercel para autorizar el cron job. |

---

## 📄 Licencia

Este proyecto está bajo la licencia MIT. Puedes usarlo, modificarlo y distribuirlo libremente.