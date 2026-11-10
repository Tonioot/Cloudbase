from typing import Optional
from pydantic import BaseModel, Field

class DeployRequest(BaseModel):
    name: str
    repo_url: str
    github_token: Optional[str] = None
    github_token_id: Optional[str] = None   # ID of a saved vault token
    domain: Optional[str] = None
    extra_domains: Optional[list] = None      # additional domains/subdomains
    redirect_domains: Optional[list] = None   # domains that redirect to primary
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    start_command: Optional[str] = None
    build_command: Optional[str] = None      # runs once during the image build
    port: Optional[int] = None               # internal port (inside container)
    docker_cpu_limit: Optional[float] = None
    docker_memory_limit_mb: Optional[int] = None
    docker_read_only_root: Optional[bool] = None
    docker_tmpfs_enabled: Optional[bool] = None
    docker_tmpfs_size_mb: Optional[int] = None
    env_vars: Optional[dict] = None
    node_id: Optional[int] = None
    restart_policy: Optional[str] = None   # no | always | on-failure
    no_web: Optional[bool] = None          # True = no web server, skip nginx + port assignment
    app_type: Optional[str] = None         # "static" = serve files with nginx (start_command = publish dir)
    source_revision: Optional[str] = None
    image_revision: Optional[str] = None
    cron_schedule: Optional[str] = None    # set = a cron job: runs start_command on this schedule
    cron_timezone: Optional[str] = None
    cron_timeout_seconds: Optional[int] = None
    health_path: Optional[str] = None             # "" = only check that the port is open
    health_expect: Optional[str] = None           # "" = any answer below 500; "2xx,301"
    health_timeout_seconds: Optional[int] = None


class UpdateRequest(BaseModel):
    name: Optional[str] = None                # renaming changes the automatic subdomain, nothing else
    repo_url: Optional[str] = None            # another repository; the next pull or deploy fetches from it
    domain: Optional[str] = None
    extra_domains: Optional[list] = None      # additional domains/subdomains
    redirect_domains: Optional[list] = None   # domains that redirect to primary
    ssl_cert_path: Optional[str] = None
    ssl_key_path: Optional[str] = None
    start_command: Optional[str] = None
    build_command: Optional[str] = None      # "" clears it
    port: Optional[int] = None               # internal port
    docker_cpu_limit: Optional[float] = None
    docker_memory_limit_mb: Optional[int] = None
    docker_read_only_root: Optional[bool] = None
    docker_tmpfs_enabled: Optional[bool] = None
    docker_tmpfs_size_mb: Optional[int] = None
    env_vars: Optional[dict] = None       # new/changed values only (write-only UI)
    env_var_keys: Optional[list] = None   # full set of remaining key names (None = keep all)
    github_token: Optional[str] = None
    github_token_id: Optional[str] = None   # ID of a saved vault token
    restart_policy: Optional[str] = None   # no | always | on-failure
    source_revision: Optional[str] = None
    image_revision: Optional[str] = None
    autoscale_enabled:      Optional[bool]  = None
    autoscale_min_instances: Optional[int]   = None
    autoscale_max_instances: Optional[int]   = None
    autoscale_cpu_target:   Optional[float] = None
    autoscale_memory_target_mb: Optional[int] = None    # 0 = off
    autoscale_up_seconds:   Optional[int]   = None
    autoscale_down_minutes: Optional[int]   = None
    autoscale_max_step_up:  Optional[int]   = None      # 0 = no limit
    autoscale_max_step_down: Optional[int]  = None      # 0 = no limit
    auto_deploy:            Optional[bool]  = None
    deploy_branch:          Optional[str]   = None   # "" = the checked-out branch
    deploy_strategy:        Optional[str]   = None   # rolling | blue_green
    auto_deploy_interval:   Optional[int]   = None   # seconds
    health_path:            Optional[str]   = None   # "" = only check that the port is open
    health_expect:          Optional[str]   = None   # "" = any answer below 500; "2xx,301"
    health_timeout_seconds: Optional[int]   = None


class MaintenancePageConfig(BaseModel):
    title: Optional[str] = ""
    message: Optional[str] = ""
    color: Optional[str] = "#f85149"
    status_url: Optional[str] = None
    custom_html: Optional[str] = None
    logo_data: Optional[str] = None    # base64 data-URL for logo image
    theme: Optional[str] = "auto"      # auto (visitor's system) | light | dark
    brand_name: Optional[str] = None   # shown next to the logo in the header
    background: Optional[str] = "none" # none | glow | grid


class MaintenanceSettings(BaseModel):
    downtime_page: MaintenancePageConfig = MaintenancePageConfig()
    update_page: MaintenancePageConfig = MaintenancePageConfig(color="#f0883e")
    restart_page: MaintenancePageConfig = MaintenancePageConfig(color="#388bfd")
    starting_page: MaintenancePageConfig = MaintenancePageConfig(color="#388bfd")


class CheckRepoRequest(BaseModel):
    repo_url: str
    github_token: Optional[str] = None
    github_token_id: Optional[str] = None


class AppCheckRepoRequest(BaseModel):
    repo_url: str


class ScaleRequest(BaseModel):
    count: int = Field(1, ge=1)  # this many more
    target: Optional[int] = Field(None, ge=1)  # or: this many in total (adds or removes the difference)
    node_id: Optional[int] = None
    docker_cpu_limit: Optional[float] = None
    docker_memory_limit_mb: Optional[int] = None
    docker_read_only_root: Optional[bool] = None
    docker_tmpfs_enabled: Optional[bool] = None
    docker_tmpfs_size_mb: Optional[int] = None


class RenderPageRequest(BaseModel):
    page_type: str
    page: MaintenancePageConfig


class GitDeployRequest(BaseModel):
    commit: Optional[str] = None          # None = latest on the current branch
    strategy: str = "rebuild"             # rebuild | rolling | blue_green
    trigger: str = "manual"               # manual | rollback
    pull: bool = True                     # False: build/deploy the source as it is (Rebuild)
