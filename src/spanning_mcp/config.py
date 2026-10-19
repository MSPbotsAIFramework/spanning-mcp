# Copyright 2026 MSPbots.ai
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Process settings. Holds no credentials: those arrive per request as headers."""

from pydantic_settings import BaseSettings, SettingsConfigDict

# Spanning Backup for Microsoft 365 runs one API host per data region.
API_URL_TEMPLATE = "https://o365-api-{region}.spanningbackup.com/external"
REGIONS = ("us", "eu", "uk", "ca", "ap", "af")
# Cheap authenticated read used to find the region an account lives in.
PROBE_PATH = "tenant"

# Platform names callers may send, mapped to the platform they mean.
PLATFORM_ALIASES = {
    "m365": "m365",
    "o365": "m365",
    "office365": "m365",
    "microsoft365": "m365",
    "microsoft": "m365",
    "gws": "gws",
    "gapps": "gws",
    "google": "gws",
    "googleworkspace": "gws",
    "gsuite": "gws",
    "salesforce": "salesforce",
    "sfdc": "salesforce",
}
SUPPORTED_PLATFORM = "m365"


class Settings(BaseSettings):
    # extra="ignore": the runtime injects variables this project does not declare.
    model_config = SettingsConfigDict(extra="ignore")

    mcp_http_port: int = 8080
    mcp_http_host: str = "0.0.0.0"
    log_level: str = "INFO"


def get_settings() -> Settings:
    return Settings()
