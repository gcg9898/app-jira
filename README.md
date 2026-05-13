# Jira Board App

Panel de gestión de tareas de Jira estilo Trello con bandeja del sistema y atajo de teclado global.

## Qué hace

- **Tablero web** (Flask + SQLite) accesible en `http://localhost:5000` con columnas tipo Kanban.
- **Sincronización con Jira**: importa tus incidencias desde un filtro de Jira y las muestra en el tablero.
- **Tray app**: icono en la bandeja del sistema con acceso rápido para crear tareas.
- **Hotkey global `Ctrl+Alt+N`**: abre un popup para crear una nueva tarea desde cualquier ventana.
- **Launcher**: panel de control para arrancar/parar Flask y la tray app, ver estado y configurar credenciales.

## Requisitos

- [uv](https://github.com/astral-sh/uv) instalado y en el PATH.
- Python gestionado por uv (no necesitas instalarlo manualmente).
- Google Chrome + ChromeDriver para las capturas de pantalla automáticas (gestionado por `webdriver-manager`).

## Configuración inicial

Crea un fichero `.env` en la raíz del proyecto con tus credenciales de Jira:

```env
JIRA_USER=tu_usuario
JIRA_PASS=tu_contraseña
JIRA_BASE_URL=https://tu-instancia.jira.com
JIRA_FILTER_ID=12345
```

### ¿De dónde saco la URL y el filtro?

Abre tu filtro de Jira en el navegador. La URL tendrá esta forma:

```
https://tu-jira.ejemplo.com/issues/?filter=30004
         ^^^^^^^^^^^^^^^^^^^^^^              ^^^^^
         JIRA_BASE_URL                       JIRA_FILTER_ID
```

- **`JIRA_BASE_URL`** → la parte de la URL hasta antes de `/issues` (ejemplo: `https://tu-jira.ejemplo.com`).
- **`JIRA_FILTER_ID`** → el número que aparece después de `filter=` (ejemplo: `30004`).

> También puedes rellenar estos campos directamente desde el launcher sin editar el fichero a mano.

## Arrancar la aplicación

### Opción 1 — Doble clic (recomendado)

Ejecutar **`JiraBoard.vbs`** (no abre ventana de consola).

### Opción 2 — Desde terminal

```bat
JiraBoard.bat
```

### Opción 3 — Manual con uv

```bash
uv run --with flask --with requests --with selenium --with webdriver-manager --with keyboard --with pystray --with pillow --link-mode=copy python launcher.py
```

## Uso

| Acción | Cómo |
|--------|------|
| Abrir el tablero web | Botón **"Abrir Board Web"** en el launcher o ir a `http://localhost:5000` |
| Sincronizar con Jira | Botón **"Sync"** en el tablero web |
| Crear tarea rápida | Atajo **`Ctrl+Alt+N`** desde cualquier ventana |
| Ver logs de Flask | Botón **"Logs"** en el launcher |
| Cambiar credenciales | Sección **"Credenciales"** en el launcher → Guardar |
| Parar todo | Botón **"Salir"** en el launcher |

## Estructura de ficheros

```
app jira/
├── app.py           # Backend Flask (API + lógica de sincronización con Jira)
├── launcher.py      # Panel de control (tkinter)
├── tray_app.py      # Icono de bandeja + hotkey Ctrl+Alt+N
├── templates/
│   └── board.html   # Frontend tablero Kanban (SPA)
├── JiraBoard.bat    # Lanzador con consola
├── JiraBoard.vbs    # Lanzador silencioso (sin consola)
├── .env             # Credenciales (NO incluido en el repositorio)
├── board.db         # Base de datos SQLite (NO incluida en el repositorio)
└── screenshots/     # Capturas automáticas de Jira (NO incluidas)
```

## Instalación del ejecutable (.exe)

El ejecutable compilado se encuentra en la carpeta **`dist/JiraBoard.exe`** del repositorio.

Para usarlo:

1. **Crea una carpeta nueva** en tu equipo donde quieras tener la aplicación (por ejemplo `C:\JiraBoard\`).
2. **Copia `JiraBoard.exe`** de `dist/` a esa carpeta.
3. **Crea el fichero `.env`** en la misma carpeta con tus credenciales (ver sección *Configuración inicial*).
4. **Ejecuta `JiraBoard.exe`** haciendo doble clic.

> **Importante:** el ejecutable genera ficheros locales (`board.db`, `screenshots/`, etc.) en la carpeta donde se encuentra, así que es necesario que tenga su propia carpeta dedicada. No lo dejes suelto en el escritorio ni en una carpeta compartida con otros archivos.

La aplicación comprueba automáticamente si hay una versión más reciente en GitHub y ofrece actualizarse.

## Notas

- El fichero `.env`, `board.db` y `screenshots/` están en `.gitignore` y **no se suben al repositorio**.
- El tablero funciona aunque no haya conexión a Jira, usando los datos locales de `board.db`.
- La primera sincronización puede tardar unos segundos dependiendo del número de incidencias en el filtro.
