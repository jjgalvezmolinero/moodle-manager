import docker
import io
import json
import os
import tarfile
import tempfile
import shutil
from typing import List
from models import Instance, InstanceStatus


def get_client():
    return docker.from_env()


def get_instance_status(instance: Instance) -> InstanceStatus:
    try:
        client = get_client()
        containers = client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={instance.compose_project_name}"},
        )
        if not containers:
            return InstanceStatus.stopped

        running = sum(1 for c in containers if c.status == "running")
        total = len(containers)

        if running == 0:
            return InstanceStatus.stopped
        elif running == total:
            return InstanceStatus.running
        else:
            return InstanceStatus.partial
    except Exception:
        return InstanceStatus.unknown


def get_instance_containers(instance: Instance) -> List[dict]:
    try:
        client = get_client()
        containers = client.containers.list(
            all=True,
            filters={"label": f"com.docker.compose.project={instance.compose_project_name}"},
        )
        result = []
        for c in containers:
            ports = []
            for container_port, host_bindings in (c.ports or {}).items():
                if host_bindings:
                    for binding in host_bindings:
                        ports.append(f"{binding['HostIp']}:{binding['HostPort']}->{container_port}")
                else:
                    ports.append(container_port)
            result.append({
                "name": c.name,
                "service": c.labels.get("com.docker.compose.service", ""),
                "status": c.status,
                "ports": ", ".join(ports) if ports else "-",
            })
        return sorted(result, key=lambda x: x["service"])
    except Exception:
        return []


def _get_container(instance: Instance, service: str):
    client = get_client()
    containers = client.containers.list(filters={
        "label": [
            f"com.docker.compose.project={instance.compose_project_name}",
            f"com.docker.compose.service={service}",
        ]
    })
    return containers[0] if containers else None


def exec_in_webserver(instance: Instance, command: str) -> tuple[int, str]:
    try:
        client = get_client()
        containers = client.containers.list(filters={
            "label": [
                f"com.docker.compose.project={instance.compose_project_name}",
                "com.docker.compose.service=webserver",
            ]
        })
        if not containers:
            return 1, "El contenedor webserver no está en ejecución."
        container = containers[0]
        exit_code, output = container.exec_run(command, demux=False)
        decoded = output.decode("utf-8", errors="replace") if output else ""
        return exit_code or 0, decoded
    except Exception as e:
        return 1, str(e)


# ── Export ─────────────────────────────────────────────────────────────────────

def _dump_db(instance: Instance) -> tuple[int, bytes]:
    """Dump the database from the db container. Returns (exit_code, sql_bytes)."""
    container = _get_container(instance, "db")
    if not container:
        return 1, b"El contenedor db no esta en ejecucion."

    db = instance.db
    if db == "pgsql":
        cmd = "pg_dump -U moodle moodle"
    elif db in ("mariadb", "mysql"):
        cmd = "mysqldump -u moodle -pm@0dl3ing --single-transaction --routines --triggers moodle"
    elif db == "mssql":
        return 1, b"Exportacion de MSSQL no soportada en esta version."
    elif db == "oracle":
        return 1, b"Exportacion de Oracle no soportada en esta version."
    else:
        return 1, f"Tipo de BD desconocido: {db}".encode()

    try:
        exit_code, (stdout, stderr) = container.exec_run(cmd, demux=True)
        if exit_code != 0:
            err = stderr.decode("utf-8", errors="replace") if stderr else "error desconocido"
            return exit_code or 1, err.encode()
        return 0, stdout or b""
    except Exception as e:
        return 1, str(e).encode()


def create_export_archive(instance: Instance) -> str:
    """Build a .tar.gz export with db dump + moodledata + instance config.

    Returns the path to a temporary tar.gz file. The caller is responsible
    for deleting it after the response is sent.
    """
    tmpdir = tempfile.mkdtemp(prefix="moodle_export_")
    try:
        export_name = instance.compose_project_name

        # 1. Instance config
        config_path = os.path.join(tmpdir, "instance.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(instance.model_dump(), f, indent=2, ensure_ascii=False)

        # 2. Database dump
        exit_code, dump_data = _dump_db(instance)
        if exit_code != 0:
            raise RuntimeError(dump_data.decode("utf-8", errors="replace"))
        dump_ext = "sql"
        dump_path = os.path.join(tmpdir, f"db_dump.{dump_ext}")
        with open(dump_path, "wb") as f:
            f.write(dump_data)

        # 3. Moodledata from webserver container
        container = _get_container(instance, "webserver")
        if container:
            chunks, _ = container.get_archive("/var/www/moodledata")
            moodle_tar_path = os.path.join(tmpdir, "moodledata.tar")
            with open(moodle_tar_path, "wb") as f:
                for chunk in chunks:
                    f.write(chunk)

        # 4. Bundle into a single tar.gz
        archive_path = os.path.join(tempfile.gettempdir(), f"{export_name}_export.tar.gz")
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(tmpdir, arcname=export_name)

        return archive_path
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
