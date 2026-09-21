import argparse
import base64
import calendar
import hashlib
import json
import os
import re
import secrets
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from dotenv import load_dotenv
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

PROJECT_DIR = Path(__file__).resolve().parent
DATA_DIR = PROJECT_DIR / 'willys_data'
RECEIPTS_DIR = PROJECT_DIR / 'willys_receipts'
API_URL = 'https://www.willys.se/axfood/rest/account/pagedOrderBonusCombined'
RECEIPT_BASE_URL = 'https://www.willys.se/axfood/rest/order/orders/digitalreceipt'
LOGIN_URL = 'https://www.willys.se/login'
LOGIN_PAGE_URL = 'https://www.willys.se/anvandare/inloggning'
END_DATE = date(2022, 1, 1)
PAGE_SIZE = 50
REQUEST_TIMEOUT = 30
RECEIPT_TIMEZONE = ZoneInfo('Europe/Stockholm')
DEFAULT_PROFILE = 'default'
PROFILE_PATTERN = re.compile(r'^[a-z0-9][a-z0-9_-]*$')

load_dotenv(PROJECT_DIR / '.env')


def _normalize_profile(profile: str) -> str:
    profile = profile.strip().lower()
    if not PROFILE_PATTERN.fullmatch(profile):
        raise ValueError(
            'Profile must contain only lowercase letters, numbers, hyphens, '
            'and underscores, and must not start with a separator.'
        )
    return profile


def _profile_credentials(profile: str) -> tuple[str, str]:
    profile = _normalize_profile(profile)
    if profile == DEFAULT_PROFILE:
        username_name = 'WILLYS_USERNAME'
        password_name = 'WILLYS_PASSWORD'
    else:
        env_profile = profile.upper().replace('-', '_')
        prefix = f'WILLYS_{env_profile}_'
        username_name = f'{prefix}USERNAME'
        password_name = f'{prefix}PASSWORD'

    username = os.environ.get(username_name, '').strip()
    password = os.environ.get(password_name, '')
    if not username or not password:
        raise RuntimeError(
            f'Set {username_name} and {password_name} in the local .env file.'
        )
    return username, password


def _profile_directories(profile: str) -> tuple[Path, Path]:
    profile = _normalize_profile(profile)
    if profile == DEFAULT_PROFILE:
        return DATA_DIR, RECEIPTS_DIR
    return DATA_DIR / profile, RECEIPTS_DIR / profile


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


def _build_session() -> requests.Session:
    """Create a session with retries for safe, idempotent API requests."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({'GET'}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)

    session = requests.Session()
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    session.headers.update(
        {
            'Accept': '*/*',
            'Content-Type': 'application/json',
            'Referer': LOGIN_PAGE_URL,
            'User-Agent': 'willys-data-exporter/1.0',
        }
    )
    return session


def login(profile: str = DEFAULT_PROFILE) -> requests.Session:
    """Authenticate one named Willys profile using the password login method."""
    username, password = _profile_credentials(profile)

    encrypted_username, username_key = _encrypt_login_value(username)
    encrypted_password, password_key = _encrypt_login_value(password)
    payload = {
        'j_username': encrypted_username,
        'j_username_key': username_key,
        'j_password': encrypted_password,
        'j_password_key': password_key,
        'j_remember_me': True,
    }

    session = _build_session()
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

    if not isinstance(result, dict):
        raise RuntimeError('Willys returned an unexpected login response.')

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
        if not isinstance(pagination, dict):
            raise RuntimeError('Willys returned invalid pagination data.')
        try:
            number_of_pages = max(1, int(pagination.get('numberOfPages') or 1))
        except (TypeError, ValueError) as exc:
            raise RuntimeError('Willys returned invalid pagination data.') from exc
        page += 1

    if first_page is None:
        raise RuntimeError('Willys returned no response data.')

    if page > 1:
        first_page['loyaltyTransactionsInPage'] = transactions
        pagination = first_page.get('paginationData', {})
        if not isinstance(pagination, dict):
            raise RuntimeError('Willys returned invalid pagination data.')
        pagination = dict(pagination)
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


def write_bytes(path: Path, data: bytes) -> None:
    """Write binary data atomically."""
    temporary_path = path.with_suffix(f'{path.suffix}.tmp')
    try:
        temporary_path.write_bytes(data)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _load_saved_response(path: Path) -> dict:
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise RuntimeError('Saved response is not a JSON object.')
    if not isinstance(data.get('loyaltyTransactionsInPage'), list):
        raise RuntimeError('Saved response has invalid transaction data.')
    return data


def _is_valid_saved_response(path: Path) -> bool:
    """Only skip existing files that look like valid API responses."""
    try:
        _load_saved_response(path)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def _is_valid_pdf(path: Path) -> bool:
    try:
        with path.open('rb') as file:
            return file.read(5) == b'%PDF-'
    except OSError:
        return False


def _receipt_date(transaction: dict) -> date:
    booking_date = transaction.get('bookingDate')
    if isinstance(booking_date, (int, float)) and not isinstance(booking_date, bool):
        try:
            return datetime.fromtimestamp(
                booking_date / 1_000, RECEIPT_TIMEZONE
            ).date()
        except (OverflowError, OSError, ValueError):
            pass

    reference = transaction.get('digitalReceiptReference')
    if isinstance(reference, str):
        try:
            return date.fromisoformat(reference[:10])
        except ValueError:
            pass

    raise RuntimeError('Transaction has no usable receipt date.')


def _receipt_filename(transaction: dict, receipt_date: date) -> str:
    reference = transaction.get('digitalReceiptReference')
    if not isinstance(reference, str) or not reference:
        raise RuntimeError('Transaction has no digital receipt reference.')

    safe_reference = re.sub(r'[^A-Za-z0-9._-]+', '_', reference).strip('._')
    if not safe_reference:
        safe_reference = hashlib.sha256(reference.encode()).hexdigest()[:16]
    return f'{receipt_date.isoformat()}_{safe_reference[:160]}.pdf'


def download_receipt(
    session: requests.Session,
    transaction: dict,
    output_path: Path,
) -> None:
    """Download one authenticated receipt PDF."""
    reference = transaction.get('digitalReceiptReference')
    store_id = transaction.get('storeCustomerId')
    source = transaction.get('receiptSource')
    member_card_number = transaction.get('memberCardNumber')
    if not all((reference, store_id, source, member_card_number)):
        raise RuntimeError('Transaction is missing receipt request fields.')

    receipt_date = _receipt_date(transaction)
    receipt_url = f'{RECEIPT_BASE_URL}/{quote(str(reference), safe="")}'
    response = session.get(
        receipt_url,
        params={
            'date': receipt_date.isoformat(),
            'storeId': str(store_id),
            'source': str(source),
            'memberCardNumber': str(member_card_number),
        },
        headers={'Accept': 'application/pdf'},
        timeout=REQUEST_TIMEOUT,
    )
    response.raise_for_status()
    if not response.content.startswith(b'%PDF-'):
        raise RuntimeError('Willys returned a non-PDF receipt response.')
    write_bytes(output_path, response.content)


def download_receipts(
    session: requests.Session,
    transactions: list,
    month_key: str,
    output_dir: Path = RECEIPTS_DIR,
) -> tuple[int, int]:
    """Download available receipt PDFs and return (downloaded, skipped)."""
    receipt_dir = output_dir / month_key
    receipt_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    skipped = 0
    failures = 0

    for transaction in transactions:
        if not isinstance(transaction, dict) or not transaction.get(
            'digitalReceiptAvailable'
        ):
            continue
        try:
            receipt_date = _receipt_date(transaction)
            path = receipt_dir / _receipt_filename(transaction, receipt_date)
            if _is_valid_pdf(path):
                skipped += 1
                continue
            download_receipt(session, transaction, path)
            downloaded += 1
        except (
            OSError,
            requests.RequestException,
            RuntimeError,
            ValueError,
        ):
            failures += 1

    if failures:
        raise RuntimeError(f'{failures} receipt download(s) failed.')
    return downloaded, skipped


def month_ranges(earliest_date: date):
    current_date = date.today()
    while current_date >= earliest_date:
        first_day = current_date.replace(day=1)
        _, days_in_month = calendar.monthrange(
            current_date.year, current_date.month
        )
        last_day = date(current_date.year, current_date.month, days_in_month)
        yield current_date, first_day, last_day
        current_date = first_day - timedelta(days=1)


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Download Willys loyalty data and receipt PDFs.'
    )
    parser.add_argument(
        '--check-login',
        action='store_true',
        help='authenticate and exit without downloading any data',
    )
    parser.add_argument(
        '--download-receipts',
        action='store_true',
        help='also download available itemized receipt PDFs',
    )
    parser.add_argument(
        '--profile',
        default=DEFAULT_PROFILE,
        help=(
            'credential and output profile (default: default); named profiles '
            'use WILLYS_<PROFILE>_USERNAME and WILLYS_<PROFILE>_PASSWORD'
        ),
    )
    args = parser.parse_args()

    try:
        profile = _normalize_profile(args.profile)
        data_dir, receipts_dir = _profile_directories(profile)
        session = login(profile)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        print(f'Setup failed: {exc}', file=sys.stderr)
        return 1

    if args.check_login:
        print('Willys login successful.')
        return 0

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f'Unable to create data directory {data_dir}: {exc}', file=sys.stderr)
        return 1

    print(f'Profile: {profile}')
    print(
        f'Starting data fetch from {date.today():%Y-%m} '
        f'to {END_DATE:%Y-%m}...'
    )
    print(f'Data will be stored in: {data_dir}')
    if args.download_receipts:
        print(f'Receipts will be stored in: {receipts_dir}')

    failures = []
    for current_date, first_day, last_day in month_ranges(END_DATE):
        filename = f'willys_{current_date:%Y-%m}.json'
        filepath = data_dir / filename
        data = None
        if filepath.exists():
            try:
                data = _load_saved_response(filepath)
                print(f'Skipping {filename} - file already exists')
            except (OSError, RuntimeError, ValueError):
                print(f'Refetching {filename} - existing file is invalid')

        if data is None:
            try:
                data = fetch_month(session, first_day, last_day)
                write_json(filepath, data)
                print(f'Fetched {filename}')
            except (
                OSError,
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as exc:
                print(f'Failed to fetch {filename}: {exc}', file=sys.stderr)
                failures.append(filename)
                continue

        if args.download_receipts:
            try:
                downloaded, skipped = download_receipts(
                    session,
                    data['loyaltyTransactionsInPage'],
                    current_date.strftime('%Y-%m'),
                    receipts_dir,
                )
                if downloaded or skipped:
                    print(
                        f'Receipts for {current_date:%Y-%m}: '
                        f'{downloaded} downloaded, {skipped} already present'
                    )
            except (
                OSError,
                requests.RequestException,
                RuntimeError,
                ValueError,
            ) as exc:
                print(
                    f'Failed receipts for {filename}: {exc}',
                    file=sys.stderr,
                )
                failures.append(f'{filename} receipts')

    if failures:
        print(f'Failed items: {", ".join(failures)}', file=sys.stderr)
        return 1

    print('Data fetch complete.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
