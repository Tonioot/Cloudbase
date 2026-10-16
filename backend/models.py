from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, Text, Boolean, ForeignKey, Float, Table, UniqueConstraint
from database import Base


# Many-to-many: roles <-> permissions
role_permissions = Table(
    "role_permissions",
    Base.metadata,
    Column("role_id", Integer, ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True),
    Column("permission_id", Integer, ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True),
)


class Permission(Base):
    __tablename__ = "permissions"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(80), unique=True, nullable=False, index=True)  # e.g. "apps.view"
    description = Column(String(200), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class Role(Base):
    __tablename__ = "roles"

    id          = Column(Integer, primary_key=True, index=True)
    name        = Column(String(50), unique=True, nullable=False, index=True)
    description = Column(String(200), nullable=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"

    id           = Column(Integer, primary_key=True, index=True)
    username     = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String, nullable=False)
    role         = Column(String(10), nullable=False, default="viewer")  # legacy, kept for JWT compat
    role_id      = Column(Integer, ForeignKey("roles.id", ondelete="SET NULL"), nullable=True, index=True)
    # Bumped on logout and password change: sessions issued before stop working
    session_version = Column(Integer, nullable=False, default=0)
    created_at   = Column(DateTime, default=datetime.utcnow)


class Application(Base):
    __tablename__ = "applications"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False)
    repo_url = Column(String(500), nullable=False)
    github_token    = Column(String(200), nullable=True)
    github_token_id = Column(String(100), nullable=True)  # vault token ID, if set via saved token
    domain = Column(String(200), nullable=True)
    extra_domains = Column(Text, nullable=True)     # JSON list of additional domains/subdomains
    redirect_domains = Column(Text, nullable=True)  # JSON list of domains that redirect to primary
    ssl_cert_path = Column(String(500), nullable=True)
    ssl_key_path = Column(String(500), nullable=True)
    app_type = Column(String(50), nullable=True)
    start_command = Column(String(500), nullable=True)
    build_command = Column(String(500), nullable=True)  # runs once during `docker build` (e.g. npm run build)
    # Auto-deploy: poll the branch and roll out new commits
    auto_deploy          = Column(Boolean, default=False, nullable=False)
    deploy_branch        = Column(String(200), nullable=True)   # None = the branch that is checked out
    deploy_strategy      = Column(String(20), default="rolling", nullable=False)  # rolling | blue_green
    auto_deploy_interval = Column(Integer, default=60, nullable=False)  # seconds between checks
    port          = Column(Integer, nullable=True)   # internal port (inside container)
    external_port = Column(Integer, nullable=True)   # host port (auto-assigned 8000–8999)
    status = Column(String(20), default="stopped")
    pid = Column(Integer, nullable=True)
    node_id = Column(Integer, ForeignKey("nodes.id"), nullable=True, index=True)
    working_dir = Column(String(500), nullable=True)
    last_error = Column(Text, nullable=True)
    env_vars = Column(Text, nullable=True)
    nginx_enabled  = Column(Boolean, default=False)
    auto_start     = Column(Boolean, default=False)
    restart_policy = Column(String(20), default="no")   # no | always | on-failure
    docker_image   = Column(String(500), nullable=True)
    source_revision = Column(String(120), nullable=True)
    image_revision  = Column(String(120), nullable=True)
    docker_cpu_limit = Column(Float, nullable=True)
    docker_memory_limit_mb = Column(Integer, nullable=True)
    docker_read_only_root = Column(Boolean, default=False)
    docker_tmpfs_enabled = Column(Boolean, default=False)
    docker_tmpfs_size_mb = Column(Integer, nullable=True)
    no_web           = Column(Boolean, default=False)   # True = no web server, skip nginx + port
    autoscale_enabled      = Column(Boolean, default=False)
    autoscale_min_replicas = Column(Integer, default=1)
    autoscale_max_replicas = Column(Integer, default=4)
    autoscale_cpu_target   = Column(Float,   default=70.0)  # % CPU to trigger scale up
    maintenance_mode = Column(Boolean, default=False)
    update_mode      = Column(Boolean, default=False)
    downtime_page    = Column(Text, nullable=True)  # JSON: {title, message, color, custom_html}
    update_page      = Column(Text, nullable=True)  # JSON: {title, message, color, custom_html}
    restart_page     = Column(Text, nullable=True)  # JSON: {title, message, color, custom_html}
    starting_page    = Column(Text, nullable=True)  # JSON: {title, message, color, custom_html}
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Node(Base):
    __tablename__ = "nodes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), unique=True, nullable=False)
    role = Column(String(20), default="node")  # main | node | hybrid
    status = Column(String(20), default="online")  # online | offline | unknown
    api_base_url = Column(String(500), nullable=True)
    public_host = Column(String(255), nullable=True)
    auth_token = Column(String(200), nullable=True, index=True)
    is_local = Column(Boolean, default=False)
    enabled = Column(Boolean, default=True)
    capabilities = Column(Text, nullable=True)  # JSON dict
    metadata_json = Column(Text, nullable=True) # JSON dict
    heartbeat_interval = Column(Integer, default=15)
    last_seen = Column(DateTime, nullable=True)
    offline_since = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    connection_type     = Column(String(20), nullable=True)   # "websocket" | "http_polling"
    agent_version       = Column(String(40), nullable=True)
    node_cpu_percent    = Column(Float, nullable=True)
    node_memory_percent = Column(Float, nullable=True)
    node_disk_percent   = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class NodeInvite(Base):
    __tablename__ = "node_invites"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String(80), unique=True, nullable=False, index=True)
    note = Column(String(200), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    node_id = Column(Integer, ForeignKey("nodes.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class NodeCommand(Base):
    __tablename__ = "node_commands"

    id = Column(Integer, primary_key=True, index=True)
    node_id = Column(Integer, ForeignKey("nodes.id"), nullable=False, index=True)
    app_id = Column(Integer, ForeignKey("applications.id"), nullable=True, index=True)
    command_type = Column(String(80), nullable=False)
    payload = Column(Text, nullable=True)  # JSON dict
    status = Column(String(20), default="queued")  # queued | in_progress | done | failed
    priority = Column(Integer, default=5)           # 1=high, 5=normal, 10=low
    idempotency_key = Column(String(100), nullable=False, unique=True, index=True)
    result = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    dispatched_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)


class ApplicationReplica(Base):
    __tablename__ = "application_replicas"

    id            = Column(Integer, primary_key=True, index=True)
    app_id        = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id       = Column(Integer, ForeignKey("nodes.id"), nullable=True, index=True)
    external_port = Column(Integer, nullable=True)
    tunnel_port   = Column(Integer, nullable=True)   # localhost port on main node (reverse tunnel)
    container_id  = Column(String(200), nullable=True)
    status        = Column(String(20), default="stopped")  # pending|starting|running|stopping|stopped|error
    substatus     = Column(String(40), nullable=True)       # building_image|creating_container|waiting — only set during starting
    last_error    = Column(Text, nullable=True)
    # Per-instance Docker resource overrides (null = use app-level defaults)
    docker_cpu_limit        = Column(Float, nullable=True)
    docker_memory_limit_mb  = Column(Integer, nullable=True)
    docker_read_only_root   = Column(Boolean, default=False)
    docker_tmpfs_enabled    = Column(Boolean, default=False)
    docker_tmpfs_size_mb    = Column(Integer, nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id        = Column(Integer, primary_key=True, index=True)
    timestamp = Column(DateTime, default=datetime.utcnow, index=True)
    action    = Column(String(80), nullable=False)
    actor     = Column(String(80), default="admin", nullable=False)
    app_id    = Column(Integer, ForeignKey("applications.id", ondelete="SET NULL"), nullable=True, index=True)
    detail    = Column(Text, nullable=True)  # JSON string


class StatsHistory(Base):
    __tablename__ = "stats_history"

    id          = Column(Integer, primary_key=True, index=True)
    app_id      = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False)
    timestamp   = Column(DateTime, default=datetime.utcnow, nullable=False)
    cpu_percent = Column(Float, nullable=False)
    memory_mb   = Column(Float, nullable=False)
    net_mb      = Column(Float, nullable=True)
    disk_mb     = Column(Float, nullable=True)


class OverviewHistory(Base):
    """Cluster-wide samples for the dashboard (every 30s, kept 7 days)."""
    __tablename__ = "overview_history"

    id            = Column(Integer, primary_key=True, index=True)
    timestamp     = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    apps_running  = Column(Integer, nullable=False, default=0)
    apps_total    = Column(Integer, nullable=False, default=0)
    instances     = Column(Integer, nullable=False, default=0)
    cpu_percent   = Column(Float, nullable=True)
    mem_used_mb   = Column(Float, nullable=True)
    mem_total_mb  = Column(Float, nullable=True)
    nodes_online  = Column(Integer, nullable=False, default=0)
    nodes_total   = Column(Integer, nullable=False, default=0)


class NodeMetricsHistory(Base):
    """Per-node cpu / memory / disk samples (every 30s, kept 7 days)."""
    __tablename__ = "node_metrics_history"

    id             = Column(Integer, primary_key=True, index=True)
    node_id        = Column(Integer, ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp      = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    cpu_percent    = Column(Float, nullable=True)
    memory_percent = Column(Float, nullable=True)
    disk_percent   = Column(Float, nullable=True)


class SystemConfig(Base):
    __tablename__ = "system_config"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(100), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)


class Deployment(Base):
    """One deploy of an app: what was deployed, how it started and how it went."""
    __tablename__ = "deployments"

    id             = Column(Integer, primary_key=True, index=True)
    app_id         = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    commit_sha     = Column(String(64), nullable=True)
    commit_message = Column(String(500), nullable=True)
    commit_author  = Column(String(200), nullable=True)
    branch         = Column(String(200), nullable=True)
    trigger        = Column(String(20), nullable=False, default="manual")   # manual | auto | rollback
    strategy       = Column(String(20), nullable=False, default="rolling")  # rolling | blue_green | rebuild | restart
    status         = Column(String(20), nullable=False, default="running")  # running | success | failed
    error          = Column(Text, nullable=True)
    log            = Column(Text, nullable=True)
    actor          = Column(String(100), nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at    = Column(DateTime, nullable=True)


class Database(Base):
    """A managed database: one container with its own data volume on one fixed node."""
    __tablename__ = "databases"

    id              = Column(Integer, primary_key=True, index=True)
    name            = Column(String(40), unique=True, nullable=False, index=True)
    engine          = Column(String(20), nullable=False)    # postgres | mysql | mariadb | redis | valkey | mongodb
    version         = Column(String(20), nullable=False)
    node_id         = Column(Integer, ForeignKey("nodes.id"), nullable=False, index=True)  # fixed, never moved automatically
    status          = Column(String(20), default="creating", nullable=False)
    # creating | starting | running | unhealthy | stopping | stopped | error | node_offline
    desired_state   = Column(String(10), default="running", nullable=False)  # running | stopped
    last_error      = Column(Text, nullable=True)
    container_id    = Column(String(200), nullable=True)
    credentials     = Column(Text, nullable=False)          # encrypted JSON: user, password, db_name
    cpu_limit       = Column(Float, nullable=True)
    memory_limit_mb = Column(Integer, nullable=True)
    # Set after the first successful start. From then on a missing data volume is an
    # error and never a reason to initialise a fresh, empty database.
    initialized_at  = Column(DateTime, nullable=True)
    # Public access through the primary (port = db_public_min + id).
    public_enabled   = Column(Boolean, default=False, nullable=False)
    public_allowlist = Column(Text, nullable=True)  # JSON list of IPs / CIDRs; empty = everyone
    public_tls       = Column(Boolean, default=True, nullable=False)  # require TLS on the public port
    # Automatic backups: "off" | "hourly" | "daily" (at backup_hour, server time).
    backup_schedule  = Column(String(10), default="daily", nullable=False)
    backup_hour      = Column(Integer, default=3, nullable=False)
    backup_retention = Column(Integer, default=7, nullable=False)  # scheduled backups to keep
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DatabaseBackup(Base):
    """A dump of a database, stored on the primary. Outlives its database."""
    __tablename__ = "database_backups"

    id             = Column(Integer, primary_key=True, index=True)
    database_id    = Column(Integer, ForeignKey("databases.id", ondelete="SET NULL"), nullable=True, index=True)
    database_name  = Column(String(40), nullable=False)
    engine         = Column(String(20), nullable=False)
    version        = Column(String(20), nullable=True)
    source_db_name = Column(String(100), nullable=True)  # logical database inside the dump
    file_name      = Column(String(300), nullable=True)  # relative to the backups directory
    size_bytes     = Column(Integer, nullable=True)
    sha256         = Column(String(64), nullable=True)
    status         = Column(String(10), default="running", nullable=False)  # running | success | failed
    trigger        = Column(String(20), nullable=False)  # manual | scheduled | pre_delete | pre_restore | upload
    error          = Column(Text, nullable=True)
    actor          = Column(String(100), nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at    = Column(DateTime, nullable=True)
    # Off-site copy in S3-compatible storage: None | uploaded | failed
    offsite_status = Column(String(10), nullable=True)
    offsite_key    = Column(String(500), nullable=True)
    offsite_error  = Column(Text, nullable=True)
    offsite_at     = Column(DateTime, nullable=True)


class DatabaseStatsHistory(Base):
    """Per-database usage samples (every minute, kept 7 days)."""
    __tablename__ = "database_stats_history"

    id          = Column(Integer, primary_key=True, index=True)
    database_id = Column(Integer, ForeignKey("databases.id", ondelete="CASCADE"), nullable=False, index=True)
    timestamp   = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    cpu_percent = Column(Float, nullable=True)
    memory_mb   = Column(Float, nullable=True)
    disk_mb     = Column(Float, nullable=True)


class DatabaseLink(Base):
    """Injects a database's connection URL into an app as an environment variable."""
    __tablename__ = "database_links"
    __table_args__ = (UniqueConstraint("app_id", "env_var_name", name="uq_database_links_app_env"),)

    id           = Column(Integer, primary_key=True, index=True)
    database_id  = Column(Integer, ForeignKey("databases.id", ondelete="CASCADE"), nullable=False, index=True)
    app_id       = Column(Integer, ForeignKey("applications.id", ondelete="CASCADE"), nullable=False, index=True)
    env_var_name = Column(String(100), nullable=False)
    created_at   = Column(DateTime, default=datetime.utcnow)
