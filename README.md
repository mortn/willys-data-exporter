# Willys loyalty data fetcher

This small Python script downloads monthly loyalty and bonus transaction data
from Willys' account API. It walks backwards from the current month to January
2022 and saves one response per month in `willys_data/`.

The scope of this project is data acquisition only. Analysis and processing can
be performed by a separate application or pipeline.

## Privacy and security

The API responses contain personal purchase information, including a name,
loyalty-card number, store information, transaction amounts, order numbers, and
receipt references. The downloaded JSON files are intentionally ignored by Git
and should not be published or shared without removing personal data.

The repository contains no credentials. Copy `.env.example` to `.env` and fill
in the credentials locally. `.env` is ignored by Git and should never be
committed or uploaded. The fetcher uses Willys' username/password login method;
it does not use Mobilt BankID.

## Requirements

- Python 3.11 or newer
- Willys credentials that support the password login method
- The packages listed in `requirements.txt`

## Setup

Create a virtual environment and install the pinned dependencies:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

Create the local credentials file:

```sh
cp .env.example .env
```

Then edit `.env`:

```dotenv
WILLYS_USERNAME=your-username
WILLYS_PASSWORD=your-password
```

Keep the file local. Do not paste credentials into the source code or commit
`.env`.

## Usage

Verify the credentials without downloading any data:

```sh
python willys_fetcher.py --check-login
```

Download missing monthly responses:

```sh
python willys_fetcher.py
```

The script skips a month when its output file already exists, so it can be run
again to fill in missing months. Output is written to `willys_data/` using names
such as `willys_2025-11.json`.

The script authenticates once, keeps the authenticated session cookies in
memory, and uses that session for the API requests. It follows the endpoint's
pagination metadata and combines all pages for a month into the saved JSON
file.

## Configuration

The earliest month is controlled by `end_date` in `willys_fetcher.py` and is
currently `2022-01-01`. Request timeouts and page size are defined near the top
of the file.

The endpoint is a private Willys account endpoint and may change or reject
credentials. This project is for personal use and makes no claim of support for
the endpoint.
