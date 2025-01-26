from abc import ABC, abstractmethod
import sys
from typing import Any, Generic, Optional, TypeVar, Union
from uuid import uuid4
import os
from loguru import logger
from playwright.async_api import (
    async_playwright,
    Playwright,
    BrowserContext,
    FileChooser,
    Download,
)

from dendrite_sdk._api.dto.authenticate_dto import AuthenticateDTO
from dendrite_sdk._api.dto.upload_auth_session_dto import UploadAuthSessionDTO
from dendrite_sdk._common.event_sync import EventSync
from dendrite_sdk._core._managers.page_manager import (
    PageManager,
)

from dendrite_sdk._core._type_spec import DownloadType
from dendrite_sdk._core.dendrite_page import DendritePage
from dendrite_sdk._common.constants import STEALTH_ARGS
from dendrite_sdk._core.models.download_interface import DownloadInterface
from dendrite_sdk._core.models.authentication import (
    AuthSession,
)
from dendrite_sdk._core.models.llm_config import LLMConfig
from dendrite_sdk._api.browser_api_client import BrowserAPIClient


class BaseDendriteBrowser(ABC, Generic[DownloadType]):
    def __init__(
        self,
        openai_api_key: Optional[str] = None,
        dendrite_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        playwright_options: Any = {
            "headless": False,
            "args": STEALTH_ARGS,
        },
    ):
        if not dendrite_api_key or dendrite_api_key == "":
            dendrite_api_key = os.environ.get("DENDRITE_API_KEY", "")
            if not dendrite_api_key or dendrite_api_key == "":
                raise Exception("Dendrite API key is required to use DendriteBrowser")

        if not anthropic_api_key or anthropic_api_key == "":
            anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if anthropic_api_key == "":
                raise Exception("Anthropic API key is required to use DendriteBrowser")

        if not openai_api_key or openai_api_key == "":
            openai_api_key = os.environ.get("OPENAI_API_KEY", "")
            if not openai_api_key or openai_api_key == "":
                raise Exception("OpenAI API key is required to use DendriteBrowser")

        self._id = uuid4().hex
        self._auth_data: Optional[AuthSession] = None
        self._dendrite_api_key = dendrite_api_key
        self._playwright_options = playwright_options
        self._active_page_manager: Optional[PageManager] = None
        self._user_id: Optional[str] = None
        self._browser_api_client = BrowserAPIClient(dendrite_api_key, self._id)
        self.playwright: Optional[Playwright] = None
        self.browser_context: Optional[BrowserContext] = None

        self._upload_handler = EventSync[FileChooser]()
        self._download_handler = EventSync[Download]()

        llm_config = LLMConfig(
            openai_api_key=openai_api_key, anthropic_api_key=anthropic_api_key
        )
        self.llm_config = llm_config

    async def __aenter__(self):
        await self._launch()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def get_active_page(self) -> DendritePage[DownloadType]:
        active_page_manager = await self._get_active_page_manager()
        return await active_page_manager.get_active_page()

    async def goto(
        self,
        url: str,
        new_page: bool = False,
        timeout: Optional[float] = 15000,
        expected_page: str = "",
    ) -> DendritePage[DownloadType]:
        active_page_manager = await self._get_active_page_manager()

        if new_page:
            active_page = await active_page_manager.new_page()
        else:
            active_page = await active_page_manager.get_active_page()
        try:
            logger.info(f"Going to {url}")
            await active_page.playwright_page.goto(url, timeout=timeout)
        except TimeoutError:
            logger.debug("Timeout when loading page but continuing anyways.")
        except Exception as e:
            logger.debug(f"Exception when loading page but continuing anyways. {e}")

        page = await active_page_manager.get_active_page()
        if expected_page != "":
            try:
                prompt = f"We are checking if we have arrived on the expected type of page. If it is apparent that we have arrived on the wrong page, output an error. Here is the description: '{expected_page}'"
                await page.ask(prompt, bool)
            except Exception as e:
                raise Exception(f"Incorrect navigation, reason: {e}")

        return page

    async def _launch(self):
        os.environ["PW_TEST_SCREENSHOT_NO_FONTS_READY"] = "1"
        self._playwright = await async_playwright().start()
        browser = await self._playwright.chromium.launch(**self._playwright_options)

        if self._auth_data:
            self.browser_context = await browser.new_context(
                storage_state=self._auth_data.to_storage_state(),
                user_agent=self._auth_data.user_agent,
            )
        else:
            self.browser_context = await browser.new_context()

        self._active_page_manager = PageManager(self, self.browser_context)
        return browser, self.browser_context, self._active_page_manager

    async def authenticate(self, domains: Union[str, list[str]]):
        dto = AuthenticateDTO(domains=domains)
        auth_session: AuthSession = await self._browser_api_client.authenticate(dto)
        self._auth_data = auth_session

    async def new_page(self) -> DendritePage[DownloadType]:
        active_page_manager = await self._get_active_page_manager()
        return await active_page_manager.new_page()

    async def add_cookies(self, cookies):
        if not self.browser_context:
            raise Exception("Browser context not initialized")

        await self.browser_context.add_cookies(cookies)

    async def close(self):
        if self.browser_context:
            if self._auth_data:
                storage_state = await self.browser_context.storage_state()
                dto = UploadAuthSessionDTO(
                    auth_data=self._auth_data, storage_state=storage_state
                )
                await self._browser_api_client.upload_auth_session(dto)
            await self.browser_context.close()

        if self._playwright:
            await self._playwright.stop()

    def _is_launched(self):
        return self.browser_context is not None

    async def _get_active_page_manager(self) -> PageManager:
        if not self._active_page_manager:
            _, _, active_page_manager = await self._launch()
            return active_page_manager

        return self._active_page_manager

    @abstractmethod
    async def _get_download(self, timeout: float = 30000) -> DownloadType:
        pass

    async def _get_filechooser(self, timeout: float = 30000) -> FileChooser:
        return await self._upload_handler.get_data(timeout=timeout)