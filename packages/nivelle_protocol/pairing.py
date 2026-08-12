from pydantic import BaseModel, Field


class PairingComplete(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    device_name: str = Field(min_length=1, max_length=100)


class PairingResult(BaseModel):
    client_id: str
    token: str
