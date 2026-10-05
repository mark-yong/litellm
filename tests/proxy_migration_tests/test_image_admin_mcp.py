import os
import shutil
import subprocess
from typing import Final

import pytest

IMAGE: Final = os.getenv("LITELLM_IMAGE")
COMPONENT: Final = os.getenv("LITELLM_IMAGE_COMPONENT", "unified")
PROBE: Final = """
import asyncio
import base64
import importlib
import json
import sys
from typing import Final

import httpx2
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException
from litellm_admin_mcp.server import create_http_app
from litellm.proxy import proxy_server

module_name: Final = {
    "unified": "litellm.proxy.proxy_server",
    "backend": "backend.main",
    "gateway": "gateway.main",
}[sys.argv[2]]
app: Final = importlib.import_module(module_name).app

if sys.argv[1] == "base":
    private_key: Final = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    message: Final = json.dumps({"expiration_date": "2999-01-01", "user_id": "image-test"}).encode()
    signature: Final = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    proxy_server._license_check.public_key = private_key.public_key()
    proxy_server._license_check.license_str = base64.b64encode(message + b"." + signature).decode()

async def probe() -> None:
    try:
        async with app.router.lifespan_context(app):
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://localhost"
            ) as client:
                response: Final = await client.post(
                    "/admin/mcp",
                    json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Accept": "application/json, text/event-stream"},
                )
                print("admin-mcp-status=" + str(response.status_code))
    except HTTPException as exc:
        assert exc.status_code == 403 and "LITELLM_LICENSE" in str(exc.detail)
        assert sys.argv[1] == "none"
        print("admin-mcp-license-required")

asyncio.run(probe())
"""

pytestmark = [
    pytest.mark.skipif(IMAGE is None, reason="requires a built image (set LITELLM_IMAGE)"),
    pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI"),
]


@pytest.mark.parametrize(
    "enabled,license_mode,expected",
    [
        (None, "none", "status=404"),
        ("false", "base", "status=404"),
        ("true", "none", "license-required"),
        ("true", "base", "status=404" if COMPONENT == "gateway" else "status=401"),
    ],
)
def test_image_admin_mcp_requires_opt_in_license_and_management_component(
    enabled: str | None, license_mode: str, expected: str
) -> None:
    assert IMAGE is not None
    enabled_args: Final = () if enabled is None else ("--env", "LITELLM_ENABLE_ADMIN_MCP=" + enabled)
    result: Final = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--user",
            "12345:0",
            *enabled_args,
            "--env",
            "LITELLM_LOCAL_MODEL_COST_MAP=true",
            "--env",
            "LITELLM_MASTER_KEY=sk-0123456789abcdef0123456789abcdef",
            "--entrypoint",
            "python",
            IMAGE,
            "-c",
            PROBE,
            license_mode,
            COMPONENT,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0 and f"admin-mcp-{expected}" in result.stdout, (
        f"Admin MCP image probe failed with component={COMPONENT}, enabled={enabled}, license={license_mode}\n"
        f"{result.stdout}\n{result.stderr}"
    )
