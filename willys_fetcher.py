import argparse
import base64
import calendar
import hashlib
import json
import os
import secrets
from datetime import date, timedelta
from pathlib import Path

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / 'willys_data'
API_URL = 'https://www.willys.se/axfood/rest/account/pagedOrderBonusCombined'
LOGIN_URL = 'https://www.willys.se/login'
LOGIN_PAGE_URL = 'https://www.willys.se/anvandare/inloggning'
PAGE_SIZE = 50
REQUEST_TIMEOUT = 30

load_dotenv(PROJECT_DIR / '.env')


def _encrypt_login_value(value: str) -> tuple[str, str]:
    """Match the browser's Willys username/password encryption format."""
    login_key = (
        f'{secrets.randbelow(100_000_000):08d}'
        f'{secrets.randbelow(100_000_000):08d}'
    )
    iv = secrets.token_bytes(16)
    salt = secrets.token_bytes(16)
    derived_key = hashlib.pbkdf2_hmac(
        'sha1', login_key.encode(), salt, 1_000, dklen=16
    )

    plaintext = value.encode()
    padding_length = 16 - (len(plaintext) % 16)
    padded_plaintext = plaintext + bytes([padding_length]) * padding_length
    encryptor = Cipher(
        algorithms.AES(derived_key), modes.CBC(iv)
    ).encryptor()
    ciphertext = encryptor.update(padded_plaintext) + encryptor.finalize()

    encrypted_value = '::'.join(
        (
            iv.hex(),
            salt.hex(),
            base64.b64encode(ciphertext).decode('ascii'),
        )
    )
    return base64.b64encode(encrypted_value.encode('ascii')).decode('ascii'), login_key


def login() -> requests.Session:
    """Authenticate with Willys using the password login method."""
    username = os.environ.get('WILLYS_USERNAME', '').strip()
    password = os.environ.get('WILLYS_PASSWORD', '')
    if not username or not password:
        raise RuntimeError(
            'Set WILLYS_USERNAME and WILLYS_PASSWORD in the local .env file.'
        )

    encrypted_username, username_key = _encrypt_login_value(username)
    encrypted_password, password_key = _encrypt_login_value(password)
    payload = {
        'j_username': encrypted_username,
        'j_username_key': username_key,
        'j_password': encrypted_password,
        'j_password_key': password_key,
        'j_remember_me': True,
    }

    session = requests.Session()
    session.headers.update(
        {
            'Accept': '*/*',
            'Content-Type': 'application/json',
            'Referer': LOGIN_PAGE_URL,
            'User-Agent': 'willys-data-fetcher/1.0',
        }
    )
    response = session.post(
        LOGIN_URL,
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    try:
        result = response.json()
    except ValueError as exc:
        raise RuntimeError('Willys returned an invalid login response.') from exc

    if str(result.get('login_successful', '')).lower() != 'true':
        message = result.get('message') or 'credentials were rejected'
        raise RuntimeError(f'Willys login failed: {message}')

    return session


def fetch_month(
    session: requests.Session,
    first_day: date,
    last_day: date,
) -> dict:
    """Fetch all pages for one month and combine them into one response."""
    first_page = None
    transactions = []
    page = 0
    number_of_pages = 1

    while page < number_of_pages:
        params = {
            'currentPage': str(page),
            'pageSize': str(PAGE_SIZE),
            'fromDate': first_day.isoformat(),
            'toDate': last_day.isoformat(),
        }
        response = session.get(
            API_URL,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise RuntimeError('Willys returned an unexpected JSON response.')

        if first_page is None:
            first_page = data

        page_transactions = data.get('loyaltyTransactionsInPage', [])
        if not isinstance(page_transactions, list):
            raise RuntimeError('Willys returned invalid transaction data.')
        transactions.extend(page_transactions)

        pagination = data.get('paginationData', {})
        try:
            number_of_pages = max(1, int(pagination.get('numberOfPages') or 1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError('Willys returned invalid pagination data.') from exc
        page += 1

    if first_page is None:
        raise RuntimeError('Willys returned no response data.')

    if page > 1:
        first_page['loyaltyTransactionsInPage'] = transactions
        pagination = dict(first_page.get('paginationData') or {})
        pagination.update(
            {
                'currentPage': 0,
                'numberOfPages': 1,
                'totalNumberOfResults': len(transactions),
                'hasNext': False,
                'hasPrevious': False,
            }
        )
        first_page['paginationData'] = pagination

    return first_page


def write_json(path: Path, data: dict) -> None:
    """Write a response atomically so an interrupted run cannot corrupt it."""
    temporary_path = path.with_suffix(f'{path.suffix}.tmp')
    try:
        temporary_path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + '\n',
            encoding='utf-8',
        )
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def month_ranges(end_date: date):
    current_date = date.today()
    while current_date >= end_date:
        first_day = current_date.replace(day=1)
        _, days_in_month = calendar.monthrange(
            current_date.year, current_date.month
        )
        last_day = date(current_date.year, current_date.month, days_in_month)
        yield current_date, first_day, last_day
        current_date = first_day - timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Download Willys loyalty data month by month.'
    )
    parser.add_argument(
        '--check-login',
        action='store_true',
        help='authenticate and exit without downloading any data',
    )
    args = parser.parse_args()

    session = login()
    if args.check_login:
        print('Willys login successful.')
        return 0

    end_date = date(2022, 1, 1)
    DATA_DIR.mkdir(exist_ok=True)
    print(
        f'Starting data fetch from {date.today():%Y-%m} '
        f'to {end_date:%Y-%m}...'
    )
    print(f'Data will be stored in: {DATA_DIR}')

    failures = []
    for current_date, first_day, last_day in month_ranges(end_date):
        filename = f'willys_{current_date:%Y-%m}.json'
        filepath = DATA_DIR / filename
        if filepath.exists():
            print(f'Skipping {filename} - file already exists')
            continue

        try:
            data = fetch_month(session, first_day, last_day)
            write_json(filepath, data)
            print(f'Fetched {filename}')
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f'Failed to fetch {filename}: {exc}')
            failures.append(filename)

    if failures:
        print(f'Failed months: {", ".join(failures)}')
        return 1

    print('Data fetch complete.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
