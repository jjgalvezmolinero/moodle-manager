import os
import json
import platform
import asyncio
from typing import List, AsyncGenerator
from models import Instance

DATA_DIR = os.environ.get("DATA_DIR", "/data")


def _get_app_runtime(version: str) -> str:
    """Determine Moodle App runtime (ionic5 or ionic7) from a semver string."""
    try:
        parts = version.split(".")
        major = int(parts[0])
        minor = int(parts[1]) if len(parts) > 1 else 0
        if major > 4 or (major == 4 and minor > 3):
            return "ionic7"
        return "ionic5"
    except (ValueError, IndexError):
        return "ionic7"


def _write_xdebug_yml(instance: Instance) -> str:
    """Generate a compose override file for Xdebug and return its path."""
    content = (
        "services:\n"
        "  webserver:\n"
        "    environment:\n"
        f'      XDEBUG_MODE: "{instance.xdebug_mode}"\n'
        f'      XDEBUG_CONFIG: "client_host={instance.xdebug_client_host} client_port={instance.xdebug_port}"\n'
    )
    overrides_dir = os.path.join(DATA_DIR, "overrides")
    os.makedirs(overrides_dir, exist_ok=True)
    path = os.path.join(overrides_dir, f"{instance.id}-xdebug.yml")
    with open(path, "w") as f:
        f.write(content)
    return path


def active_services(instance: Instance) -> List[str]:
    """Return the list of service names to pass to 'docker compose up'."""
    services = ["webserver", "db"]
    if instance.start_mail:
        services.append("mailpit")
    if instance.start_selenium:
        services.append("selenium")
    if instance.start_exttests:
        services.append("exttests")
    return services


def build_compose_files(instance: Instance) -> List[str]:
    base = instance.moodle_docker_path
    files = [
        f"{base}/base.yml",
        f"{base}/service.mail.yml",
        f"{base}/db.{instance.db}.yml",
    ]

    if instance.db_version:
        version_file = f"{base}/db.{instance.db}.{instance.db_version}.yml"
        if os.path.exists(version_file):
            files.append(version_file)

    if instance.db_port:
        port_file = f"{base}/db.{instance.db}.port.yml"
        if os.path.exists(port_file):
            files.append(port_file)

    browser_name = instance.browser.split(":")[0]

    if browser_name == "chrome":
        if instance.app_path:
            files.append(f"{base}/moodle-app-dev.yml")
        elif instance.app_version:
            files.append(f"{base}/moodle-app.yml")

    if browser_name != "firefox":
        chrome_file = f"{base}/selenium.{browser_name}.yml"
        if os.path.exists(chrome_file):
            files.append(chrome_file)

    if instance.selenium_vnc_port:
        files.append(f"{base}/selenium.debug.yml")

    if instance.phpunit_external_services:
        files.append(f"{base}/phpunit-external-services.yml")

    if instance.bbb_mock:
        files.append(f"{base}/bbb-mock.yml")

    if instance.matrix_mock:
        files.append(f"{base}/matrix-mock.yml")

    if instance.mlbackend:
        files.append(f"{base}/mlbackend.yml")

    if instance.behat_faildump:
        files.append(f"{base}/behat-faildump.yml")

    if instance.web_port:
        files.append(f"{base}/webserver.port.yml")

    if platform.system() == "Darwin":
        files.append(f"{base}/volumes-cached.yml")

    local = f"{base}/local.yml"
    if os.path.exists(local):
        files.append(local)

    if instance.xdebug:
        files.append(_write_xdebug_yml(instance))

    return files


def build_env(instance: Instance) -> dict:
    env = os.environ.copy()
    browser_parts = instance.browser.split(":")
    browser_name = browser_parts[0]
    browser_tag = browser_parts[1] if len(browser_parts) > 1 else "4"

    web_port = instance.web_port
    if ":" not in web_port:
        web_port = f"0.0.0.0:{web_port}"

    env.update({
        "COMPOSE_PROJECT_NAME": instance.compose_project_name,
        "MOODLE_DOCKER_WWWROOT": instance.wwwroot,
        "MOODLE_DOCKER_DB": instance.db,
        "MOODLE_DOCKER_PHP_VERSION": instance.php_version,
        "MOODLE_DOCKER_WEB_HOST": instance.web_host,
        "MOODLE_DOCKER_WEB_PORT": web_port,
        "MOODLE_DOCKER_TIMEOUT_FACTOR": str(instance.timeout_factor),
        "MOODLE_DOCKER_BROWSER": instance.browser,
        "MOODLE_DOCKER_BROWSER_NAME": browser_name,
        "MOODLE_DOCKER_BROWSER_TAG": browser_tag,
        "MOODLE_DOCKER_SELENIUM_SUFFIX": "",
        "ASSETDIR": f"{instance.moodle_docker_path}/assets",
    })

    if instance.db_version:
        env["MOODLE_DOCKER_DB_VERSION"] = instance.db_version

    if instance.db_port:
        db_port = instance.db_port
        if ":" not in db_port:
            db_port = f"127.0.0.1:{db_port}"
        env["MOODLE_DOCKER_DB_PORT"] = db_port

    if instance.selenium_vnc_port:
        vnc = instance.selenium_vnc_port
        if ":" not in vnc:
            vnc = f"127.0.0.1:{vnc}"
        env["MOODLE_DOCKER_SELENIUM_VNC_PORT"] = vnc

    if instance.phpunit_external_services:
        env["MOODLE_DOCKER_PHPUNIT_EXTERNAL_SERVICES"] = "true"
    if instance.bbb_mock:
        env["MOODLE_DOCKER_BBB_MOCK"] = "true"
    if instance.matrix_mock:
        env["MOODLE_DOCKER_MATRIX_MOCK"] = "true"
    if instance.mlbackend:
        env["MOODLE_DOCKER_MLBACKEND"] = "true"
    if instance.behat_faildump:
        env["MOODLE_DOCKER_BEHAT_FAILDUMP"] = instance.behat_faildump
    if instance.app_path:
        env["MOODLE_DOCKER_APP_PATH"] = instance.app_path
    if instance.app_version:
        env["MOODLE_DOCKER_APP_VERSION"] = instance.app_version

    # App mobile: derive runtime, port, protocol and node version
    if instance.app_path or instance.app_version:
        app_version = instance.app_version or ""
        if instance.app_path and not app_version:
            try:
                pkg = json.load(open(os.path.join(instance.app_path, "package.json")))
                app_version = pkg.get("version", "")
            except Exception:
                pass

        runtime = _get_app_runtime(app_version) if app_version else "ionic7"
        env["MOODLE_DOCKER_APP_RUNTIME"] = runtime

        protocol = "http" if runtime == "ionic5" else "https"
        env["MOODLE_DOCKER_APP_PROTOCOL"] = protocol

        if instance.app_version:
            # Docker image: port depends on runtime
            env["MOODLE_DOCKER_APP_PORT"] = "80" if runtime == "ionic5" else "443"

        if instance.app_path:
            # Local dev: node version from model field or .nvmrc
            node_version = instance.app_node_version
            if not node_version:
                try:
                    raw = open(os.path.join(instance.app_path, ".nvmrc")).read().strip()
                    node_version = raw.lstrip("v").replace("/", "-")
                except Exception:
                    node_version = "18"
            env["MOODLE_DOCKER_APP_NODE_VERSION"] = node_version

    # Selenium: use -debug image suffix for tags older than 4 when VNC is enabled
    if instance.selenium_vnc_port:
        browser_parts = instance.browser.split(":")
        tag = browser_parts[1] if len(browser_parts) > 1 else "4"
        try:
            if int(tag.split(".")[0]) < 4:
                env["MOODLE_DOCKER_SELENIUM_SUFFIX"] = "-debug"
        except ValueError:
            pass

    return env


def build_cmd(instance: Instance, *args) -> List[str]:
    files = build_compose_files(instance)
    cmd = ["docker", "compose"]
    for f in files:
        cmd += ["-f", f]
    cmd += list(args)
    return cmd


async def run_async(instance: Instance, *args) -> tuple[int, str, str]:
    cmd = build_cmd(instance, *args)
    env = build_env(instance)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", errors="replace"), stderr.decode("utf-8", errors="replace")


async def stream_logs(instance: Instance, service: str = "webserver", tail: int = 200) -> AsyncGenerator[str, None]:
    cmd = build_cmd(instance, "logs", "-f", "--tail", str(tail), service)
    env = build_env(instance)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async for line in proc.stdout:
            yield line.decode("utf-8", errors="replace")
    finally:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()
