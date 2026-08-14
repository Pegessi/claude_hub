"""REST API for environment-variable preset persistence.

All endpoints are authenticated and return / accept JSON. Preset text values
(which may contain API tokens) are included in responses but are **never**
logged by the backend.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from ..auth.dependencies import get_current_user
from ..models import (
    EnvPreset,
    EnvPresetBulkImport,
    EnvPresetCreate,
    EnvPresetHiddenRequest,
    EnvPresetsResponse,
    EnvPresetUpdate,
    User,
)
from ..services.env_presets import BUILT_IN_PRESET_IDS, env_preset_manager

router = APIRouter(prefix="/api/env-presets", tags=["env-presets"])


def _state_to_response() -> EnvPresetsResponse:
    state = env_preset_manager.list_presets()
    return EnvPresetsResponse(
        custom_presets=[EnvPreset(**p) for p in state["custom_presets"]],
        hidden_builtin_ids=list(state["hidden_builtin_ids"]),
    )


@router.get("", response_model=EnvPresetsResponse)
async def list_env_presets(
    current_user: User = Depends(get_current_user),
) -> EnvPresetsResponse:
    """Return all custom presets and hidden built-in preset IDs."""
    return _state_to_response()


@router.post("", response_model=EnvPreset, status_code=status.HTTP_201_CREATED)
async def create_env_preset(
    body: EnvPresetCreate,
    current_user: User = Depends(get_current_user),
) -> EnvPreset:
    """Create a new custom env preset (id auto-generated)."""
    if not body.name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name must not be blank",
        )
    preset = env_preset_manager.create_preset(name=body.name, text=body.text)
    return preset


@router.put("/{preset_id}", response_model=EnvPreset)
async def upsert_env_preset(
    preset_id: str,
    body: EnvPresetCreate,
    current_user: User = Depends(get_current_user),
) -> EnvPreset:
    """Create or update a custom env preset by id (idempotent upsert, used by sync)."""
    if preset_id in BUILT_IN_PRESET_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot overwrite built-in preset",
        )
    if not body.name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name must not be blank",
        )
    preset = env_preset_manager.upsert_preset(preset_id=preset_id, name=body.name, text=body.text)
    return preset


@router.patch("/{preset_id}", response_model=EnvPreset)
async def update_env_preset(
    preset_id: str,
    body: EnvPresetUpdate,
    current_user: User = Depends(get_current_user),
) -> EnvPreset:
    """Update fields of an existing custom env preset."""
    if preset_id in BUILT_IN_PRESET_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot modify built-in preset",
        )
    if body.name is not None and not body.name.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="name must not be blank",
        )
    preset = env_preset_manager.update_preset(
        preset_id=preset_id,
        name=body.name,
        text=body.text,
    )
    if preset is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Preset '{preset_id}' not found",
        )
    return preset


@router.delete("/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_env_preset(
    preset_id: str,
    current_user: User = Depends(get_current_user),
) -> None:
    """Delete a custom env preset. Returns 404 if not found or is a built-in
    (use the hide endpoint for built-ins)."""
    if preset_id in BUILT_IN_PRESET_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Use the hide endpoint for built-in presets",
        )
    if not env_preset_manager.delete_preset(preset_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Preset '{preset_id}' not found",
        )


@router.put("/hidden/{preset_id}", status_code=status.HTTP_204_NO_CONTENT)
async def set_builtin_hidden(
    preset_id: str,
    body: EnvPresetHiddenRequest,
    current_user: User = Depends(get_current_user),
) -> None:
    """Hide or unhide a built-in preset."""
    if not env_preset_manager.set_hidden(preset_id, body.hidden):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"'{preset_id}' is not a known built-in preset",
        )


@router.post("/bulk-import", response_model=EnvPresetsResponse)
async def bulk_import_env_presets(
    body: EnvPresetBulkImport,
    current_user: User = Depends(get_current_user),
) -> EnvPresetsResponse:
    """Merge-import presets from a client (used for one-time localStorage migration).

    Existing server-side presets are preserved (backend-wins merge)."""
    state = env_preset_manager.bulk_import(body)
    return EnvPresetsResponse(
        custom_presets=[EnvPreset(**p) for p in state["custom_presets"]],
        hidden_builtin_ids=list(state["hidden_builtin_ids"]),
    )
