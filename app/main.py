import os
import re
import json
import threading
import asyncio
from fastapi import FastAPI, Request, Form, HTTPException, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse
from datetime import datetime
from typing import Optional, Annotated

from urllib.parse import quote as _urlquote

from models import Instance, InstanceStatus, DBType
import store
from compose import run_async, stream_logs, active_services, MOODLE_DOCKER_PATH
from docker_ops import get_instance_status, get_instance_containers, exec_in_webserver, create_export_archive
from paths import to_internal, to_host, is_drive_root, join_host, HOST_BROWSE_ROOT, IS_WINDOWS

app = FastAPI(title="Moodle Manager")
templates = Jinja2Templates(directory="templates")

PHP_VERSIONS = ["8.4", "8.3", "8.2", "8.1", "8.0", "7.4", "7.3", "7.2", "7.1", "7.0"]
DB_TYPES = [e.value for e in DBType]

STATUS_LABELS = {
    InstanceStatus.running: ("Activa", "bg-green-500"),
    InstanceStatus.partial: ("Parcial", "bg-yellow-500"),
    InstanceStatus.stopped: ("Detenida", "bg-slate-400"),
    InstanceStatus.unknown: ("Desconocido", "bg-gray-300"),
}

templates.env.filters["urlencode"] = lambda s: _urlquote(str(s), safe="")

templates.env.globals.update({
    "STATUS_LABELS": STATUS_LABELS,
    "InstanceStatus": InstanceStatus,
    "HOST_BROWSE_ROOT": HOST_BROWSE_ROOT,
    "IS_WINDOWS": IS_WINDOWS,
    "now": lambda: datetime.now().strftime("%H:%M:%S"),
})


def _empty_to_none(value: Optional[str]) -> Optional[str]:
    if value is None or value.strip() == "":
        return None
    return value.strip()


def _parse_instance_form(
    name, compose_project_name, wwwroot, db,
    php_version, db_version, web_port, web_host, db_port, browser,
    selenium_vnc_port, phpunit_external_services, bbb_mock, matrix_mock,
    mlbackend, behat_faildump, timeout_factor, app_path, app_version,
    app_node_version, notes,
    start_mail, start_selenium, start_exttests,
    xdebug_mode, xdebug_client_host, xdebug_port,
) -> dict:
    return dict(
        name=name.strip(),
        compose_project_name=compose_project_name.strip(),
        wwwroot=wwwroot.strip(),
        db=db,
        php_version=php_version,
        db_version=_empty_to_none(db_version),
        web_port=web_port.strip() or "8000",
        web_host=web_host.strip() or "localhost",
        db_port=_empty_to_none(db_port),
        browser=browser.strip() or "firefox",
        selenium_vnc_port=_empty_to_none(selenium_vnc_port),
        start_mail=start_mail is not None,
        start_selenium=start_selenium is not None,
        start_exttests=start_exttests is not None,
        xdebug_mode=xdebug_mode.strip() or "develop,debug",
        xdebug_client_host=xdebug_client_host.strip() or "host.docker.internal",
        xdebug_port=int(xdebug_port) if xdebug_port else 9003,
        phpunit_external_services=phpunit_external_services is not None,
        bbb_mock=bbb_mock is not None,
        matrix_mock=matrix_mock is not None,
        mlbackend=mlbackend is not None,
        behat_faildump=_empty_to_none(behat_faildump),
        timeout_factor=int(timeout_factor) if timeout_factor else 1,
        app_path=_empty_to_none(app_path),
        app_version=_empty_to_none(app_version),
        app_node_version=_empty_to_none(app_node_version),
        notes=_empty_to_none(notes),
    )


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    instances = store.get_all()
    rows = [{"instance": i, "status": get_instance_status(i)} for i in instances]
    return templates.TemplateResponse("index.html", {"request": request, "rows": rows})


# ── Status fragments (polling HTMX) ──────────────────────────────────────────

@app.get("/instances/{instance_id}/status-badge", response_class=HTMLResponse)
async def status_badge(request: Request, instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    status = get_instance_status(instance)
    label, color = STATUS_LABELS[status]
    return HTMLResponse(
        f'<span id="badge-{instance_id}" '
        f'class="inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-xs font-medium text-white {color}" '
        f'hx-get="/instances/{instance_id}/status-badge" hx-trigger="every 6s" hx-swap="outerHTML">'
        f'<span class="w-1.5 h-1.5 rounded-full bg-white/70"></span>{label}</span>'
    )


@app.get("/instances/{instance_id}/containers-fragment", response_class=HTMLResponse)
async def containers_fragment(request: Request, instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    containers = get_instance_containers(instance)
    return templates.TemplateResponse("fragments/containers.html", {
        "request": request, "containers": containers, "instance": instance,
    })


# ── Create / Edit ─────────────────────────────────────────────────────────────

@app.get("/instances/new", response_class=HTMLResponse)
async def new_form(request: Request):
    return templates.TemplateResponse("form.html", {
        "request": request,
        "instance": None,
        "php_versions": PHP_VERSIONS,
        "db_types": DB_TYPES,
    })


@app.post("/instances", response_class=HTMLResponse)
async def create_instance(
    request: Request,
    name: Annotated[str, Form()],
    compose_project_name: Annotated[str, Form()],
    wwwroot: Annotated[str, Form()],
    db: Annotated[str, Form()],
    php_version: Annotated[str, Form()] = "8.3",
    db_version: Annotated[Optional[str], Form()] = None,
    web_port: Annotated[str, Form()] = "8000",
    web_host: Annotated[str, Form()] = "localhost",
    db_port: Annotated[Optional[str], Form()] = None,
    browser: Annotated[str, Form()] = "firefox",
    selenium_vnc_port: Annotated[Optional[str], Form()] = None,
    start_mail: Annotated[Optional[str], Form()] = "1",
    start_selenium: Annotated[Optional[str], Form()] = None,
    start_exttests: Annotated[Optional[str], Form()] = None,
    xdebug_mode: Annotated[str, Form()] = "develop,debug",
    xdebug_client_host: Annotated[str, Form()] = "host.docker.internal",
    xdebug_port: Annotated[Optional[str], Form()] = "9003",
    phpunit_external_services: Annotated[Optional[str], Form()] = None,
    bbb_mock: Annotated[Optional[str], Form()] = None,
    matrix_mock: Annotated[Optional[str], Form()] = None,
    mlbackend: Annotated[Optional[str], Form()] = None,
    behat_faildump: Annotated[Optional[str], Form()] = None,
    timeout_factor: Annotated[Optional[str], Form()] = "1",
    app_path: Annotated[Optional[str], Form()] = None,
    app_version: Annotated[Optional[str], Form()] = None,
    app_node_version: Annotated[Optional[str], Form()] = None,
    notes: Annotated[Optional[str], Form()] = None,
):
    data = _parse_instance_form(
        name, compose_project_name, wwwroot, db,
        php_version, db_version, web_port, web_host, db_port, browser,
        selenium_vnc_port, phpunit_external_services, bbb_mock, matrix_mock,
        mlbackend, behat_faildump, timeout_factor, app_path, app_version,
        app_node_version, notes,
        start_mail, start_selenium, start_exttests,
        xdebug_mode, xdebug_client_host, xdebug_port,
    )
    instance = Instance(**data)
    store.save(instance)
    return RedirectResponse(f"/instances/{instance.id}", status_code=303)


@app.get("/instances/{instance_id}/edit", response_class=HTMLResponse)
async def edit_form(request: Request, instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse("form.html", {
        "request": request,
        "instance": instance,
        "php_versions": PHP_VERSIONS,
        "db_types": DB_TYPES,
    })


@app.post("/instances/{instance_id}/edit", response_class=HTMLResponse)
async def update_instance(
    request: Request,
    instance_id: str,
    name: Annotated[str, Form()],
    compose_project_name: Annotated[str, Form()],
    wwwroot: Annotated[str, Form()],
    db: Annotated[str, Form()],
    php_version: Annotated[str, Form()] = "8.3",
    db_version: Annotated[Optional[str], Form()] = None,
    web_port: Annotated[str, Form()] = "8000",
    web_host: Annotated[str, Form()] = "localhost",
    db_port: Annotated[Optional[str], Form()] = None,
    browser: Annotated[str, Form()] = "firefox",
    selenium_vnc_port: Annotated[Optional[str], Form()] = None,
    start_mail: Annotated[Optional[str], Form()] = "1",
    start_selenium: Annotated[Optional[str], Form()] = None,
    start_exttests: Annotated[Optional[str], Form()] = None,
    xdebug_mode: Annotated[str, Form()] = "develop,debug",
    xdebug_client_host: Annotated[str, Form()] = "host.docker.internal",
    xdebug_port: Annotated[Optional[str], Form()] = "9003",
    phpunit_external_services: Annotated[Optional[str], Form()] = None,
    bbb_mock: Annotated[Optional[str], Form()] = None,
    matrix_mock: Annotated[Optional[str], Form()] = None,
    mlbackend: Annotated[Optional[str], Form()] = None,
    behat_faildump: Annotated[Optional[str], Form()] = None,
    timeout_factor: Annotated[Optional[str], Form()] = "1",
    app_path: Annotated[Optional[str], Form()] = None,
    app_version: Annotated[Optional[str], Form()] = None,
    app_node_version: Annotated[Optional[str], Form()] = None,
    notes: Annotated[Optional[str], Form()] = None,
):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    data = _parse_instance_form(
        name, compose_project_name, wwwroot, db,
        php_version, db_version, web_port, web_host, db_port, browser,
        selenium_vnc_port, phpunit_external_services, bbb_mock, matrix_mock,
        mlbackend, behat_faildump, timeout_factor, app_path, app_version,
        app_node_version, notes,
        start_mail, start_selenium, start_exttests,
        xdebug_mode, xdebug_client_host, xdebug_port,
    )
    updated = instance.model_copy(update=data)
    store.save(updated)
    return RedirectResponse(f"/instances/{instance_id}", status_code=303)


@app.post("/instances/{instance_id}/delete")
async def delete_instance(instance_id: str):
    store.delete(instance_id)
    return RedirectResponse("/", status_code=303)


# ── Instance detail ───────────────────────────────────────────────────────────

@app.get("/instances/{instance_id}", response_class=HTMLResponse)
async def instance_detail(request: Request, instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    status = get_instance_status(instance)
    containers = get_instance_containers(instance)
    return templates.TemplateResponse("instance.html", {
        "request": request,
        "instance": instance,
        "status": status,
        "containers": containers,
    })


# ── Compose actions ───────────────────────────────────────────────────────────

async def _compose_action(instance_id: str, *args) -> JSONResponse:
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    returncode, stdout, stderr = await run_async(instance, *args)
    ok = returncode == 0
    output = stdout or stderr
    return JSONResponse({"ok": ok, "output": output.strip()})


def _disable_config_debug(content: str) -> str:
    """Comment out `$CFG->debug = ...` in config.php.

    With debug set in config.php Moodle considers itself in developer mode
    before the DB is available and skips the component cache, rescanning every
    plugin directory on each request (~8 s per page on a Windows bind mount).
    The developer level is stored in the DB instead (see _set_developer_debug).
    """
    return re.sub(
        r"^(\s*)(\$CFG->debug\s*=)",
        r"\1// Debug se guarda en la BD (Moodle Manager): en config.php desactiva la caché de componentes.\n\1// \2",
        content,
        flags=re.MULTILINE,
    )


def _set_developer_debug(instance: Instance) -> tuple[int, str]:
    """Store DEBUG_DEVELOPER (E_ALL) in the DB. debugdisplay stays in
    config.php: it does not affect the component cache."""
    return exec_in_webserver(instance, ["php", "admin/cli/cfg.php", "--name=debug", "--set=32767"])


@app.post("/instances/{instance_id}/up")
async def compose_up(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)

    messages = []

    # Copy config.php from moodle-docker template if not present
    src = os.path.join(MOODLE_DOCKER_PATH, "config.docker-template.php")
    dst = os.path.join(to_internal(instance.wwwroot), "config.php")
    if os.path.isfile(src) and not os.path.isfile(dst):
        try:
            with open(src, encoding="utf-8") as f:
                content = f.read()
            with open(dst, "w", encoding="utf-8", newline="\n") as f:
                f.write(_disable_config_debug(content))
            messages.append("config.php copiado desde la plantilla.")
        except Exception as e:
            messages.append(f"Aviso: no se pudo copiar config.php: {e}")

    services = active_services(instance)
    returncode, stdout, stderr = await run_async(instance, "up", "-d", *services)
    ok = returncode == 0
    output = "\n".join(messages)
    if stdout.strip():
        output += "\n" + stdout.strip()
    if stderr.strip():
        output += "\n" + stderr.strip()
    return JSONResponse({"ok": ok, "output": output.strip()})


@app.post("/instances/{instance_id}/stop")
async def compose_stop(instance_id: str):
    return await _compose_action(instance_id, "stop")


@app.post("/instances/{instance_id}/down")
async def compose_down(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    returncode, stdout, stderr = await run_async(instance, "down")
    ok = returncode == 0
    if ok:
        store.delete(instance_id)
    output = (stdout or stderr).strip()
    return JSONResponse({"ok": ok, "output": output, "redirect": "/" if ok else None})


@app.post("/instances/{instance_id}/restart")
async def compose_restart(instance_id: str):
    return await _compose_action(instance_id, "restart")


@app.post("/instances/{instance_id}/pull")
async def compose_pull(instance_id: str):
    return await _compose_action(instance_id, "pull")


# ── Log streaming (SSE) ───────────────────────────────────────────────────────

@app.get("/instances/{instance_id}/logs")
async def logs_stream(request: Request, instance_id: str, service: str = "webserver"):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)

    async def generator():
        async for line in stream_logs(instance, service):
            if await request.is_disconnected():
                break
            yield {"data": line.rstrip(), "event": "message"}

    return EventSourceResponse(generator())


# ── Moodle actions ────────────────────────────────────────────────────────────

@app.post("/instances/{instance_id}/actions/install-db")
async def action_install_db(
    instance_id: str,
    lang: Annotated[str, Form()] = "es",
    adminuser: Annotated[str, Form()] = "admin",
    adminpass: Annotated[str, Form()] = "Admin1234!",
    adminemail: Annotated[str, Form()] = "admin@example.com",
    fullname: Annotated[str, Form()] = "Moodle Dev",
    shortname: Annotated[str, Form()] = "moodle",
):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    url = instance.web_url()
    cmd = (
        f"php admin/cli/install_database.php"
        f" --lang={lang}"
        f" --wwwroot={url}"
        f" --dataroot=/var/www/moodledata"
        f" --adminuser={adminuser}"
        f" --adminpass={adminpass}"
        f" --adminemail={adminemail}"
        f" --fullname='{fullname}'"
        f" --shortname={shortname}"
        f" --agree-license"
    )
    exit_code, output = exec_in_webserver(instance, cmd)
    if exit_code == 0:
        dbg_code, dbg_output = _set_developer_debug(instance)
        output += "\nDebug DEVELOPER activado en la BD." if dbg_code == 0 else f"\nAviso: no se pudo activar el debug: {dbg_output}"
    return JSONResponse({"ok": exit_code == 0, "output": output.strip()})


@app.post("/instances/{instance_id}/actions/init-phpunit")
async def action_init_phpunit(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    exit_code, output = exec_in_webserver(instance, "php admin/tool/phpunit/cli/init.php")
    return JSONResponse({"ok": exit_code == 0, "output": output.strip()})


@app.post("/instances/{instance_id}/actions/init-behat")
async def action_init_behat(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    exit_code, output = exec_in_webserver(instance, "php admin/tool/behat/cli/init.php")
    return JSONResponse({"ok": exit_code == 0, "output": output.strip()})


@app.post("/instances/{instance_id}/actions/fast-config")
async def action_fast_config(instance_id: str):
    """Move the debug setting of an existing config.php to the DB."""
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    path = os.path.join(to_internal(instance.wwwroot), "config.php")
    try:
        with open(path, encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        return JSONResponse({"ok": False, "output": f"No se pudo leer config.php: {e}"})

    new_content = _disable_config_debug(content)
    if new_content == content:
        return JSONResponse({"ok": True, "output": "config.php ya estaba optimizado: no define $CFG->debug."})

    # cfg.php refuses to change a setting defined in config.php, so the line
    # goes first; if the DB update fails the original file is restored.
    def _write(text):
        with open(path, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    _write(new_content)
    exit_code, output = await asyncio.to_thread(_set_developer_debug, instance)
    if exit_code != 0:
        _write(content)
        return JSONResponse({"ok": False, "output": f"No se pudo guardar el debug en la BD (¿está instalada?). config.php no se ha modificado.\n{output}"})
    return JSONResponse({"ok": True, "output": "Debug DEVELOPER guardado en la BD y $CFG->debug comentado en config.php."})


@app.post("/instances/{instance_id}/actions/purge-caches")
async def action_purge_caches(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    exit_code, output = exec_in_webserver(instance, "php admin/cli/purge_caches.php")
    return JSONResponse({"ok": exit_code == 0, "output": output.strip()})


# ── Xdebug actions ────────────────────────────────────────────────────────────

# Xdebug is toggled by creating/removing its ini file and reloading Apache, so
# when it is off the extension is not loaded at all and costs nothing. Recent
# moodle-php-apache images already ship xdebug.so; older ones get it via PECL
# the first time it is enabled. Everything lives in the container, so a `down`
# leaves the new container with Xdebug off.
XDEBUG_INI = "/usr/local/etc/php/conf.d/zz-xdebug.ini"
# Written by earlier versions of the manager (pecl + docker-php-ext-enable).
XDEBUG_LEGACY_INI = "/usr/local/etc/php/conf.d/docker-php-ext-xdebug.ini"

_RELOAD_APACHE = "apache2ctl graceful >/dev/null 2>&1"
_XDEBUG_SO = '"$(php -r \'echo ini_get("extension_dir");\')/xdebug.so"'


def _xdebug_pecl_package(php_version: str) -> str:
    """PECL package compatible with the PHP version (xdebug.org/docs/compat)."""
    try:
        major, minor = [int(x) for x in php_version.split(".")[:2]]
    except ValueError:
        major, minor = 8, 0
    if major >= 8:
        return "xdebug"
    if major == 7 and minor >= 3:
        return "xdebug-3.1.6"
    if major == 7:
        return "xdebug-2.9.8"
    return "xdebug-2.5.5"


def _xdebug_ini(instance: Instance) -> str:
    """Ini content for the instance. Debugging starts on every request while
    Xdebug is on: toggling it from the manager is the trigger."""
    lines = ["zend_extension=xdebug"]
    if _xdebug_pecl_package(instance.php_version).startswith("xdebug-2"):
        lines += [
            "xdebug.remote_enable = 1",
            "xdebug.remote_autostart = 1",
            f"xdebug.remote_host = {instance.xdebug_client_host}",
            f"xdebug.remote_port = {instance.xdebug_port}",
        ]
    else:
        lines += [
            f"xdebug.mode = {instance.xdebug_mode}",
            "xdebug.start_with_request = yes",
            f"xdebug.client_host = {instance.xdebug_client_host}",
            f"xdebug.client_port = {instance.xdebug_port}",
        ]
    return "\n".join(lines) + "\n"


def _xdebug_status(instance: Instance) -> dict:
    script = f"test -f {_XDEBUG_SO} && echo installed; php -m | grep -qix xdebug && echo enabled; true"
    exit_code, output = exec_in_webserver(instance, ["sh", "-c", script])
    if exit_code != 0:
        return {"running": False, "installed": False, "enabled": False}
    return {"running": True, "installed": "installed" in output, "enabled": "enabled" in output}


@app.get("/instances/{instance_id}/xdebug")
async def xdebug_status(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    return JSONResponse(await asyncio.to_thread(_xdebug_status, instance))


@app.post("/instances/{instance_id}/xdebug/enable")
async def xdebug_enable(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    pecl_pkg = _xdebug_pecl_package(instance.php_version)
    script = (
        f"if [ ! -f {_XDEBUG_SO} ]; then "
        f"pecl channel-update pecl.php.net && pecl install {pecl_pkg} || exit 1; fi; "
        f"rm -f {XDEBUG_LEGACY_INI}; "
        f'printf "%s" "$XDEBUG_INI_CONTENT" > {XDEBUG_INI} && {_RELOAD_APACHE}'
    )
    exit_code, output = await asyncio.to_thread(
        exec_in_webserver, instance, ["sh", "-c", script],
        {"XDEBUG_INI_CONTENT": _xdebug_ini(instance)},
    )
    status = await asyncio.to_thread(_xdebug_status, instance)
    ok = exit_code == 0 and status["enabled"]
    return JSONResponse({"ok": ok, "output": "" if ok else output.strip(), **status})


@app.post("/instances/{instance_id}/xdebug/disable")
async def xdebug_disable(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    script = f"rm -f {XDEBUG_INI} {XDEBUG_LEGACY_INI} && {_RELOAD_APACHE}"
    exit_code, output = await asyncio.to_thread(exec_in_webserver, instance, ["sh", "-c", script])
    status = await asyncio.to_thread(_xdebug_status, instance)
    ok = exit_code == 0 and not status["enabled"]
    return JSONResponse({"ok": ok, "output": "" if ok else output.strip(), **status})


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "moodle_docker_path": to_host(MOODLE_DOCKER_PATH),
    })


@app.websocket("/instances/{instance_id}/terminal")
async def terminal_ws(websocket: WebSocket, instance_id: str, service: str = "webserver"):
    import docker as docker_module
    await websocket.accept()

    instance = store.get(instance_id)
    if not instance:
        await websocket.send_text("\r\nInstancia no encontrada.\r\n")
        await websocket.close()
        return

    client = docker_module.from_env()
    containers = client.containers.list(filters={
        "label": [
            f"com.docker.compose.project={instance.compose_project_name}",
            f"com.docker.compose.service={service}",
        ]
    })
    if not containers:
        await websocket.send_text(f"\r\nEl contenedor '{service}' no está en ejecución.\r\n")
        await websocket.close()
        return

    container = containers[0]

    exec_id = client.api.exec_create(
        container.id, ["/bin/bash"],
        stdin=True, tty=True, stdout=True, stderr=True,
        environment={"TERM": "xterm-256color"},
    )
    exec_sock = client.api.exec_start(exec_id["Id"], detach=False, tty=True, socket=True)

    loop = asyncio.get_event_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _read_socket():
        try:
            while True:
                chunk = exec_sock.read(4096)
                if not chunk:
                    break
                loop.call_soon_threadsafe(queue.put_nowait, chunk)
        except Exception:
            pass
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=_read_socket, daemon=True).start()

    async def send_output():
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            try:
                await websocket.send_bytes(chunk)
            except Exception:
                break

    async def recv_input():
        while True:
            try:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if "bytes" in msg:
                    exec_sock._sock.send(msg["bytes"])
                elif "text" in msg:
                    try:
                        ctrl = json.loads(msg["text"])
                        if ctrl.get("type") == "resize":
                            client.api.exec_resize(
                                exec_id["Id"],
                                height=ctrl.get("rows", 24),
                                width=ctrl.get("cols", 80),
                            )
                    except (json.JSONDecodeError, ValueError):
                        exec_sock._sock.send(msg["text"].encode())
            except WebSocketDisconnect:
                break
            except Exception:
                break

    tasks = [asyncio.create_task(send_output()), asyncio.create_task(recv_input())]
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for t in pending:
        t.cancel()
    try:
        exec_sock.close()
    except Exception:
        pass


# ── Directory browser ─────────────────────────────────────────────────────────

@app.get("/browse-dir", response_class=HTMLResponse)
async def browse_dir(request: Request, path: str = "", hx_target: str = "dir-browser-content"):
    import pathlib
    root = pathlib.Path(to_internal(HOST_BROWSE_ROOT))
    try:
        p = pathlib.Path(to_internal(path.strip() or HOST_BROWSE_ROOT)).resolve()
    except Exception:
        p = root

    if not p.is_dir():
        p = p.parent if p.parent.is_dir() else root

    # Paths are returned in host form (C:\... on Windows) so the user sees and
    # stores the paths they know.
    current_host = to_host(str(p))
    dirs = []
    try:
        for entry in sorted(p.iterdir(), key=lambda e: e.name.lower()):
            if entry.is_dir() and not entry.name.startswith("."):
                dirs.append({"name": entry.name, "path": join_host(current_host, entry.name)})
    except PermissionError:
        pass

    has_parent = p != p.parent and not is_drive_root(str(p))
    parent_path = to_host(str(p.parent)) if has_parent else None

    return templates.TemplateResponse("fragments/dir_browser.html", {
        "request": request,
        "current_path": current_host,
        "parent_path": parent_path,
        "dirs": dirs,
        "hx_target": hx_target,
    })


# ── Export ────────────────────────────────────────────────────────────────────

@app.get("/instances/{instance_id}/export")
async def export_instance(instance_id: str, background_tasks: BackgroundTasks):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)

    loop = asyncio.get_event_loop()
    try:
        archive_path = await loop.run_in_executor(None, create_export_archive, instance)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))

    def _cleanup():
        try:
            os.remove(archive_path)
        except OSError:
            pass

    background_tasks.add_task(_cleanup)
    filename = f"{instance.compose_project_name}_export.tar.gz"
    return FileResponse(
        path=archive_path,
        media_type="application/gzip",
        filename=filename,
        background=background_tasks,
    )


# ── Moodle clone ──────────────────────────────────────────────────────────────

_moodle_versions_cache: list | None = None

_FALLBACK_VERSIONS = [
    "MOODLE_405_STABLE", "MOODLE_404_STABLE", "MOODLE_403_STABLE",
    "MOODLE_402_STABLE", "MOODLE_401_STABLE", "MOODLE_400_STABLE",
    "MOODLE_311_STABLE", "MOODLE_310_STABLE",
]


async def _fetch_moodle_versions() -> list:
    proc = await asyncio.create_subprocess_exec(
        "git", "ls-remote", "--heads", "https://github.com/moodle/moodle.git",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    branches = []
    for line in stdout.decode().splitlines():
        m = re.search(r'refs/heads/(MOODLE_(\d)(\d+)_STABLE)$', line)
        if m:
            major, minor = int(m.group(2)), int(m.group(3))
            if major > 3 or (major == 3 and minor >= 9):
                branches.append((m.group(1), major, minor))
    branches.sort(key=lambda x: (x[1], x[2]), reverse=True)
    return [b[0] for b in branches] if branches else _FALLBACK_VERSIONS


@app.get("/moodle/versions")
async def moodle_versions():
    global _moodle_versions_cache
    if _moodle_versions_cache is not None:
        return JSONResponse(_moodle_versions_cache)
    try:
        result = await asyncio.wait_for(_fetch_moodle_versions(), timeout=15.0)
    except Exception:
        result = _FALLBACK_VERSIONS
    _moodle_versions_cache = result
    return JSONResponse(result)


@app.get("/moodle/clone")
async def moodle_clone(request: Request, path: str, branch: str):
    path = path.strip()
    branch = branch.strip()
    internal_path = to_internal(path)

    async def generator():
        if os.path.exists(internal_path):
            yield {"data": json.dumps({"ok": False, "error": f"La ruta ya existe: {path}"}), "event": "done"}
            return

        cmd = [
            "git", "clone", "--depth=1", "--progress",
            "--branch", branch,
            "https://github.com/moodle/moodle.git",
            internal_path,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as e:
            yield {"data": json.dumps({"ok": False, "error": str(e)}), "event": "done"}
            return

        buf = b""
        async for chunk in proc.stdout:
            if await request.is_disconnected():
                proc.kill()
                return
            buf += chunk
            parts = re.split(rb'[\r\n]+', buf)
            buf = parts[-1]
            for part in parts[:-1]:
                text = part.decode("utf-8", errors="replace").strip()
                if text:
                    yield {"data": text, "event": "message"}

        if buf.strip():
            yield {"data": buf.decode("utf-8", errors="replace").strip(), "event": "message"}

        await proc.wait()
        ok = proc.returncode == 0
        yield {"data": json.dumps({"ok": ok, "path": path}), "event": "done"}

    return EventSourceResponse(generator())


@app.get("/health")
async def health():
    return {"status": "ok"}
