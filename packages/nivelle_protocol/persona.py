from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from .identity import (
    CALL_NAME,
    DEFAULT_LORE,
    DEFAULT_PERSONA_DIRECTIVES,
    DEFAULT_RELATIONSHIP,
    DEFAULT_ROLE,
    DEFAULT_TONE,
    FULL_CHARACTER_NAME,
    KOREAN_FULL_NAME,
    PERSONA_VERSION,
    PRODUCT_NAME,
    USER_NAME,
)


class StrictPersonaModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


class PersonaIdentitySettings(StrictPersonaModel):
    name: Annotated[str, Field(min_length=1, max_length=100)] = PRODUCT_NAME
    full_name: Annotated[str, Field(min_length=1, max_length=100)] = FULL_CHARACTER_NAME
    korean_full_name: Annotated[str, Field(min_length=1, max_length=100)] = KOREAN_FULL_NAME
    call_name: Annotated[str, Field(min_length=1, max_length=100)] = CALL_NAME
    profile_version: Annotated[str, Field(min_length=1, max_length=20)] = PERSONA_VERSION
    role: Annotated[str, Field(min_length=1, max_length=300)] = DEFAULT_ROLE
    user_name: Annotated[str, Field(min_length=1, max_length=100)] = USER_NAME
    user_address: Annotated[str, Field(min_length=1, max_length=100)] = USER_NAME
    default_language: Annotated[str, Field(min_length=1, max_length=32)] = "ko"
    tone: Annotated[str, Field(min_length=1, max_length=500)] = DEFAULT_TONE
    relationship_description: Annotated[str, Field(min_length=1, max_length=1_000)] = (
        DEFAULT_RELATIONSHIP
    )
    lore: Annotated[str, Field(min_length=1, max_length=1_000)] = DEFAULT_LORE


class PersonaBehaviorSettings(StrictPersonaModel):
    everyday_conversation: Annotated[str, Field(min_length=1, max_length=1_000)] = (
        "자연스럽고 간결하게 대화한다. 잡담에서 해결책만 제시하지 않고 침묵도 대화로 받아들인다."
    )
    technical_work: Annotated[str, Field(min_length=1, max_length=1_000)] = (
        "감정보다 정확성과 실행 가능성을 우선하며, 더 나은 방법은 이유와 함께 제안한다."
    )
    correction_style: Annotated[str, Field(min_length=1, max_length=1_000)] = (
        "오류를 직접적이되 예의 있게 바로잡는다."
    )
    praise_style: Annotated[str, Field(min_length=1, max_length=1_000)] = (
        "구체적인 근거가 있을 때만 절제해 칭찬하며 억지 위로나 과장된 감탄을 하지 않는다."
    )
    persona_directives: Annotated[str, Field(min_length=1, max_length=10_000)] = (
        DEFAULT_PERSONA_DIRECTIVES
    )
    verbosity: Annotated[str, Field(min_length=1, max_length=100)] = "간결"
    humor: Annotated[str, Field(min_length=1, max_length=100)] = "절제됨"
    avoid_excessive_flattery: bool = True
    user_correction_priority: bool = True


class PersonaSettings(StrictPersonaModel):
    identity: PersonaIdentitySettings = PersonaIdentitySettings()
    behavior: PersonaBehaviorSettings = PersonaBehaviorSettings()


__all__ = [
    "PersonaBehaviorSettings",
    "PersonaIdentitySettings",
    "PersonaSettings",
]
