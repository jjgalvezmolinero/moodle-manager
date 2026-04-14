import docker
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
