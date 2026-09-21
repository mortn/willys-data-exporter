# Willys data exporter

This small Python script exports monthly loyalty and bonus transaction data
from [Willys](https://www.willys.se/)' account API. It walks backwards from
the current month to January
2022 and saves one response per month in `willys_data/`. It can also download
Willys' original itemized receipt PDFs into `willys_receipts/`.

The scope of this project is data acquisition only. Analysis and processing can
be performed by a separate application or pipeline.

## Privacy and security

The API responses and receipt PDFs contain personal purchase information,
including a name, loyalty-card number, store information, transaction amounts,
order numbers, receipt references, individual products, quantities, and prices.
The downloaded JSON and PDF files are intentionally ignored by Git and should
not be published or shared without removing personal data.

The repository contains no credentials. Copy `.env.example` to `.env` and fill
in the credentials locally. `.env` is ignored by Git and should never be
committed or uploaded. The exporter uses Willys' username/password login method;
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

## Multiple accounts

The exporter supports separate named profiles. Each profile gets its own login
session and output directories. For example, add these variables to `.env`:

```dotenv
WILLYS_USER1_USERNAME=your-username
WILLYS_USER1_PASSWORD=your-password
WILLYS_USER2_USERNAME=your-username
WILLYS_USER2_PASSWORD=your-password
```

Use the profiles separately:

```sh
python willys_exporter.py --profile user1 --download-receipts
python willys_exporter.py --profile user2 --download-receipts
```

The output is isolated as follows:

```text
willys_data/user1/
willys_data/user2/
willys_receipts/user1/
willys_receipts/user2/
```

Profile names are aliases, not account names. They must contain only letters,
numbers, hyphens, and underscores. The original unprefixed
`WILLYS_USERNAME`/`WILLYS_PASSWORD` variables remain available as the default
profile and use the original root output directories.

## Usage

Verify the credentials without downloading any data:

```sh
python willys_exporter.py --check-login
```

Download missing monthly responses:

```sh
python willys_exporter.py
```

Download the monthly responses and the available itemized receipt PDFs for
the default profile:

```sh
python willys_exporter.py --download-receipts
```

The JSON responses are written to `willys_data/` using names such as
`willys_2025-11.json`. Receipt PDFs are written to
`willys_receipts/2025-11/`. The receipt files are the original documents from
Willys; this project does not parse or transform them. A separate processing
pipeline can extract product lines, quantities, prices, VAT, and totals.

The script skips a month when its JSON file already exists and valid receipt
files when `--download-receipts` is used, so it can be run again to fill in
missing data. It authenticates once, keeps the authenticated session cookies in
memory, and uses that session for the API and receipt requests. It follows the
endpoint's pagination metadata and combines all pages for a month into the
saved JSON file. Safe API GET requests are retried for transient server and
rate-limit responses.

Run the offline test suite with:

```sh
python -m unittest discover -s tests -v
```

## Configuration

The earliest month is controlled by `END_DATE` in `willys_exporter.py` and is
currently `2022-01-01`. Request timeouts and page size are defined near the top
of the file.

The endpoint is a private Willys account endpoint and may change or reject
credentials. This project is for personal use and makes no claim of support for
the endpoint.
