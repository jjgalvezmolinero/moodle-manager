from pydantic import BaseModel, Field
from typing import Optional
from enum import StrEnum
from datetime import datetime
import uuid


class DBType(StrEnum):
    pgsql = "pgsql"
    mariadb = "mariadb"
    mysql = "mysql"
    mssql = "mssql"
    oracle = "oracle"


class InstanceStatus(StrEnum):
    running = "running"
    partial = "partial"
    stopped = "stopped"
    unknown = "unknown"


class Instance(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    moodle_docker_path: str
    compose_project_name: str
    wwwroot: str
    db: DBType
    php_version: str = "8.3"
    db_version: Optional[str] = None
    web_port: str = "8000"
    web_host: str = "localhost"
    db_port: Optional[str] = None
    browser: str = "firefox"
    selenium_vnc_port: Optional[str] = None
    # Servicios a arrancar
    start_mail: bool = True
    start_selenium: bool = False
    start_exttests: bool = False
    # Xdebug
    xdebug: bool = False
    xdebug_mode: str = "develop,debug"
    xdebug_client_host: str = "host.docker.internal"
    xdebug_port: int = 9003
    # Servicios opcionales
    phpunit_external_services: bool = False
    bbb_mock: bool = False
    matrix_mock: bool = False
    mlbackend: bool = False
    behat_faildump: Optional[str] = None
    timeout_factor: int = 1
    app_path: Optional[str] = None
    app_version: Optional[str] = None
    app_node_version: Optional[str] = None
    notes: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())

    def web_url(self) -> str:
        port = self.web_port.split(":")[-1]
        return f"http://{self.web_host}:{port}"
