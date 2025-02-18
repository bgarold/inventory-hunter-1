import asyncio
import getpass
import logging
import os
import pathlib
import random
import re
import requests
import shutil
import string
import subprocess

from abc import ABC, abstractmethod
from selenium import webdriver
from worker.worker_pb2 import Request, Response


user_agent = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/90.0.4427.0 Safari/537.36'


class HttpGetResponse:
    def __init__(self, text, url, **kwargs):
        self.text = text
        self.url = url
        self.status_code = kwargs.get('status_code', None)


class Driver(ABC):
    def __init__(self, **kwargs):
        self.data_dir = kwargs.get('data_dir')
        self.timeout = kwargs.get('timeout')

    @abstractmethod
    def get(self, url) -> HttpGetResponse:
        pass


class SeleniumDriver(Driver):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.selenium_path = pathlib.Path('selenium').resolve()
        self.selenium_path.mkdir(exist_ok=True)
        self.driver_path = self.selenium_path / 'chromedriver'
        driver_paths = [
            '/usr/bin/chromedriver',
            '/usr/local/bin/chromedriver',
        ]
        for driver_path in driver_paths:
            if os.path.exists(driver_path):
                # chromedriver needs to be patched to avoid detection, see:
                # https://stackoverflow.com/questions/33225947/can-a-website-detect-when-you-are-using-selenium-with-chromedriver
                shutil.copy(driver_path, self.driver_path)
                with open(driver_path, 'rb') as f:
                    variables = set([m.decode('ascii') for m in re.findall(b'cdc_[^\' ]+', f.read())])
                    for v in variables:
                        replacement = ''.join(random.choice(string.ascii_letters) for i in range(len(v)))
                        logging.debug(f'found variable in chromedriver: {v}, replacing with {replacement}')
                        cmd = ['perl', '-pi', '-e', f's/{v}/{replacement}/g', self.driver_path]
                        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, text=True)
                        if r.returncode != 0:
                            logging.warning(f'chromedriver patch failed: {r.stdout}')
                break

        if not self.driver_path.is_file():
            raise Exception(f'Selenium Chrome driver not found at {" or ".join(driver_paths)}')

        self.options = webdriver.ChromeOptions()
        self.options.page_load_strategy = 'eager'
        if getpass.getuser() == 'root':
            self.options.add_argument('--no-sandbox')  # required if root
        self.options.add_argument('--headless')
        self.options.add_argument('--disable-blink-features=AutomationControlled')
        self.options.add_argument('--disable-dev-shm-usage')
        self.options.add_argument(f'--user-agent="{user_agent}"')
        self.options.add_argument(f'--user-data-dir={self.selenium_path}')
        self.options.add_argument('--window-position=0,0')
        self.options.add_argument('--window-size=1920,1080')

    def get(self, url) -> HttpGetResponse:
        # headless chromium crashes somewhat regularly...
        # for now, we will start a fresh instance every time
        with webdriver.Chrome(self.driver_path, options=self.options) as driver:
            driver.get(str(url))

            try:
                filename = self.data_dir / f'{url.nickname}.png'
                driver.save_screenshot(str(filename))
            except Exception as e:
                logging.warning(f'unable to save screenshot of webpage: {e}')

            return HttpGetResponse(driver.page_source, url)


class PuppeteerDriver(Driver):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.script_path = pathlib.Path(__file__).parent.absolute() / 'scrape.js'
        if not self.script_path.exists():
            raise Exception(f'does not exist: {self.script_path}')

    def get(self, url) -> HttpGetResponse:
        html_file = self.data_dir / f'{url.nickname}.html'
        png_file = self.data_dir / f'{url.nickname}.png'
        cmd = ['node', self.script_path, str(url), html_file, png_file]
        r = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False, text=True)
        if r.returncode != 0:
            logging.warning(f'puppeteer scrape failed: {r.stdout}')
        else:
            with open(html_file, 'r') as f:
                content = f.read()
                return HttpGetResponse(content, url)


class RequestsDriver(Driver):
    def get(self, url) -> HttpGetResponse:
        headers = {'user-agent': user_agent, 'referer': 'https://google.com'}
        r = requests.get(str(url), headers=headers, timeout=self.timeout)
        if not r.ok:
            logging.debug(f'got response with status code {r.status_code} for {url}')
        return HttpGetResponse(r.text, r.url, status_code=r.status_code)


class LeanAndMeanDriver(Driver):
    def get(self, url) -> HttpGetResponse:
        return asyncio.run(self.get_impl(url))

    async def get_impl(self, url) -> HttpGetResponse:
        reader, writer = await asyncio.open_connection('127.0.0.1', 3080)
        request = Request()
        request.id = 1337  # doesn't matter right now
        request.url = str(url)
        request.timeout = self.timeout
        writer.write(request.SerializeToString())
        writer.write_eof()
        await writer.drain()

        response = Response()
        response.ParseFromString(await reader.read())
        logging.debug(f'got response with id {response.id}, status_code: {response.status_code}, data: <{len(response.data)} bytes>')
        writer.close()
        await writer.wait_closed()

        return HttpGetResponse(response.data, url, status_code=response.status_code)


class DriverRepo:
    def __init__(self, timeout):
        self.data_dir = pathlib.Path('data').resolve()
        self.data_dir.mkdir(exist_ok=True)
        self.requests = RequestsDriver(data_dir=self.data_dir, timeout=timeout)
        self.selenium = SeleniumDriver(data_dir=self.data_dir, timeout=timeout)
        self.puppeteer = PuppeteerDriver(data_dir=self.data_dir, timeout=timeout)
        self.lean_and_mean = LeanAndMeanDriver(data_dir=self.data_dir, timeout=timeout)


def init_drivers(config):
    timeout = int(max(config.refresh_interval, 15))  # in seconds
    return DriverRepo(timeout)
