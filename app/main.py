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
    xdebug, xdebug_mode, xdebug_client_host, xdebug_port,
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
        xdebug=xdebug is not None,
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
    xdebug: Annotated[Optional[str], Form()] = None,
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
        xdebug, xdebug_mode, xdebug_client_host, xdebug_port,
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
    xdebug: Annotated[Optional[str], Form()] = None,
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
        xdebug, xdebug_mode, xdebug_client_host, xdebug_port,
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


@app.post("/instances/{instance_id}/up")
async def compose_up(instance_id: str):
    import shutil
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)

    messages = []

    # Copy config.php from moodle-docker template if not present
    src = os.path.join(MOODLE_DOCKER_PATH, "config.docker-template.php")
    dst = os.path.join(to_internal(instance.wwwroot), "config.php")
    if os.path.isfile(src) and not os.path.isfile(dst):
        try:
            shutil.copy2(src, dst)
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


@app.post("/instances/{instance_id}/actions/purge-caches")
async def action_purge_caches(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    exit_code, output = exec_in_webserver(instance, "php admin/cli/purge_caches.php")
    return JSONResponse({"ok": exit_code == 0, "output": output.strip()})


# ── Xdebug actions ────────────────────────────────────────────────────────────

def _xdebug_install_cmd(instance) -> str:
    """Build the bash command to install and configure Xdebug for the instance's PHP version.

    Compatibility matrix (xdebug.org/docs/compat):
      PHP >= 8.0       → xdebug (3.x latest)   config: mode / client_host / client_port
      PHP 7.3, 7.4     → xdebug-3.1.6          config: mode / client_host / client_port
      PHP 7.0–7.2      → xdebug-2.9.8          config: remote_enable / remote_host / remote_port
      PHP 5.6          → xdebug-2.5.5          config: remote_enable / remote_host / remote_port
    """
    try:
        major, minor = [int(x) for x in instance.php_version.split(".")[:2]]
    except ValueError:
        major, minor = 8, 0

    if major >= 8:
        pecl_pkg = "xdebug"
        config = (
            f"xdebug.mode = {instance.xdebug_mode}\\n"
            f"xdebug.client_host = {instance.xdebug_client_host}\\n"
            f"xdebug.client_port = {instance.xdebug_port}\\n"
        )
    elif major == 7 and minor >= 3:
        pecl_pkg = "xdebug-3.1.6"
        config = (
            f"xdebug.mode = {instance.xdebug_mode}\\n"
            f"xdebug.client_host = {instance.xdebug_client_host}\\n"
            f"xdebug.client_port = {instance.xdebug_port}\\n"
        )
    elif major == 7:  # 7.0, 7.1, 7.2
        pecl_pkg = "xdebug-2.9.8"
        config = (
            f"xdebug.remote_enable = 1\\n"
            f"xdebug.remote_host = {instance.xdebug_client_host}\\n"
            f"xdebug.remote_port = {instance.xdebug_port}\\n"
        )
    else:  # PHP 5.6
        pecl_pkg = "xdebug-2.5.5"
        config = (
            f"xdebug.remote_enable = 1\\n"
            f"xdebug.remote_host = {instance.xdebug_client_host}\\n"
            f"xdebug.remote_port = {instance.xdebug_port}\\n"
        )

    ini_path = "/usr/local/etc/php/conf.d/docker-php-ext-xdebug.ini"
    return (
        f"bash -c '"
        f"pecl channel-update pecl.php.net 2>&1"
        f" && pecl install {pecl_pkg} 2>&1"
        f" && docker-php-ext-enable xdebug 2>&1"
        f" && printf \"{config}\" >> {ini_path}'"
    )


@app.post("/instances/{instance_id}/actions/install-xdebug")
async def action_install_xdebug(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    cmd = _xdebug_install_cmd(instance)
    exit_code, output = exec_in_webserver(instance, cmd)
    if exit_code == 0:
        await run_async(instance, "restart", "webserver")
    return JSONResponse({"ok": exit_code == 0, "output": output.strip()})


@app.post("/instances/{instance_id}/actions/enable-xdebug")
async def action_enable_xdebug(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    ini_path = "/usr/local/etc/php/conf.d/docker-php-ext-xdebug.ini"
    cmd = f"bash -c 'sed -i \"s/^; zend_extension=/zend_extension=/\" {ini_path} && apache2ctl graceful 2>&1'"
    exit_code, output = exec_in_webserver(instance, cmd)
    return JSONResponse({"ok": exit_code == 0, "output": output.strip() or "Xdebug activado."})


@app.post("/instances/{instance_id}/actions/disable-xdebug")
async def action_disable_xdebug(instance_id: str):
    instance = store.get(instance_id)
    if not instance:
        raise HTTPException(status_code=404)
    ini_path = "/usr/local/etc/php/conf.d/docker-php-ext-xdebug.ini"
    cmd = f"bash -c 'sed -i \"s/^zend_extension=/; zend_extension=/\" {ini_path} && apache2ctl graceful 2>&1'"
    exit_code, output = exec_in_webserver(instance, cmd)
    return JSONResponse({"ok": exit_code == 0, "output": output.strip() or "Xdebug desactivado."})


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
