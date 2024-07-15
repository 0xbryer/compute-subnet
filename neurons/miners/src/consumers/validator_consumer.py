import logging
import time
from typing import Annotated

import bittensor
from datura.consumers.base import BaseConsumer
from datura.requests.miner_requests import (
    AcceptSSHKeyRequest,
    FailedRequest,
    UnAuthorizedRequest,
)
from datura.requests.validator_requests import (
    AuthenticateRequest,
    BaseValidatorRequest,
    SSHPubKeySubmitRequest,
)
from fastapi import Depends, WebSocket

from core.config import settings
from services.ssh_service import MinerSSHService
from services.validator_service import ValidatorService

AUTH_MESSAGE_MAX_AGE = 10
MAX_MESSAGE_COUNT = 10

logger = logging.getLogger(__name__)


class ValidatorConsumer(BaseConsumer):
    def __init__(
        self,
        websocket: WebSocket,
        validator_key: str,
        ssh_service: Annotated[MinerSSHService, Depends(MinerSSHService)],
        validator_service: Annotated[ValidatorService, Depends(ValidatorService)],
    ):
        super().__init__(websocket)
        self.ssh_service = ssh_service
        self.validator_service = validator_service
        self.validator_key = validator_key
        self.my_hotkey = settings.get_bittensor_wallet().get_hotkey().ss58_address
        self.validator_authenticated = False
        self.msg_queue = []

    def accepted_request_type(self):
        return BaseValidatorRequest

    def verify_auth_msg(self, msg: AuthenticateRequest) -> tuple[bool, str]:
        if msg.payload.timestamp < time.time() - AUTH_MESSAGE_MAX_AGE:
            return False, "msg too old"
        if msg.payload.miner_hotkey != self.my_hotkey:
            return False, f"wrong miner hotkey ({self.my_hotkey}!={msg.payload.miner_hotkey})"
        if msg.payload.validator_hotkey != self.validator_key:
            return (
                False,
                f"wrong validator hotkey ({self.validator_key}!={msg.payload.validator_hotkey})",
            )

        keypair = bittensor.Keypair(ss58_address=self.validator_key)
        if keypair.verify(msg.blob_for_signing(), msg.signature):
            return True, ""

    async def handle_authentication(self, msg: AuthenticateRequest):
        # check if validator is registered
        if not self.validator_service.is_valid_validator(self.validator_key):
            await self.send_message(UnAuthorizedRequest(details="Validator is not registered"))
            await self.disconnect()
            return

        authenticated, error_msg = self.verify_auth_msg(msg)
        if not authenticated:
            response_msg = f"Validator {self.validator_key} not authenticated due to: {error_msg}"
            logger.info(response_msg)
            await self.send_message(UnAuthorizedRequest(details=response_msg))
            await self.disconnect()
            return

        self.validator_authenticated = True
        for msg in self.msg_queue:
            await self.handle_message(msg)

    async def handle_message(self, msg: BaseValidatorRequest):
        if isinstance(msg, AuthenticateRequest):
            await self.handle_authentication(msg)
            return

        # TODO: update logic here, fow now, it sends AcceptJobRequest regardless
        # if self.validator_authenticated:
        #     await self.send_message(AcceptJobRequest())

        if not self.validator_authenticated:
            if len(self.msg_queue) <= MAX_MESSAGE_COUNT:
                self.msg_queue.append(msg)
            return

        if isinstance(msg, SSHPubKeySubmitRequest):
            logger.info("Validator %s sent SSH Pubkey.", self.validator_key)

            try:
                self.ssh_service.add_pubkey_to_host(msg.public_key)
                await self.send_message(
                    AcceptSSHKeyRequest(ssh_username=self.ssh_service.get_current_os_user())
                )
                logger.info("Sent AcceptSSHKeyRequest to validator %s", self.validator_key)
            except Exception as e:
                logger.error("Storing SSH key or Sending AcceptSSHKeyRequest failed: %s", str(e))
                await self.send_message(FailedRequest(details=str(e)))
            return
