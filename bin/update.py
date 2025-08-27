# pylint: disable=too-many-lines
import csv
from dataclasses import dataclass, field, fields, replace
import datetime as dt
from enum import Enum, StrEnum
import heapq
import io
import re
import sys
import json
from pathlib import Path
import time
from typing import TypeVar
from urllib.request import HTTPError, Request, urlopen
from zoneinfo import ZoneInfo

from html_table_takeout import Table, parse_html


T = TypeVar('T')


_VERSION = '1.1.1'
_USER_AGENT = f"Sp500ComponentsHistoryBot/{_VERSION} (https://github.com/lawcal/sp500-components-history)"


COMPONENTS_HISTORY_FILE_NAME = 'components_history.csv'
CHANGELOG_FILE_NAME = 'CHANGELOG.txt'
CSV_FILE_NAME = 'sp500_components.csv'
JSON_FILE_NAME = 'sp500_components.json'


_PAGE_URL_BASE = 'https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid='
_RE_EXTRACT_TABLES = re.compile(r'(?:security|symbol|ticker)', re.IGNORECASE)
_RE_PAGE_TIMESTAMP = re.compile(r'This page was last edited on (.+?), at ([0-9]{2}:[0-9]{2})', re.IGNORECASE)
_RE_REVISION_ID = re.compile(r'oldid=([0-9]+)\D')
_RE_SYMBOL = re.compile(r'\A(?:-|\.|\/|\^|_|[A-Z]){1,10}\Z') # Allowed characters: capital letters and -./^_


# pylint: disable=line-too-long
_REVISION_API_BASE = 'https://api.wikimedia.org/core/v1/wikipedia/en/page/List_of_S%26P_500_companies/history?newer_than='
_FIRST_REVISION_WITH_SYMBOLS = '112958830'


_TIMEZONE_NEW_YORK = ZoneInfo('America/New_York')
_YYYY_MM_DD = '%Y-%m-%d'
_DATE_MIN = dt.date(1, 1, 1)
_DATE_MAX = dt.date(3000, 1, 1)


@dataclass
class Revision:
    timestamp: dt.datetime
    id: str = ''

    def __repr__(self):
        return f"{self.ny_date().strftime(_YYYY_MM_DD)} ({self.id})"

    def ny_date(self):
        return self.timestamp.astimezone(_TIMEZONE_NEW_YORK).date()


class UpdateError(ValueError):
    revision: Revision | None

    def __init__(self, *args, **kwargs):
        revision = kwargs.pop('revision', None)
        super().__init__(*args, **kwargs)
        self.revision = (
            revision if isinstance(revision, Revision)
            else Revision(timestamp=dt.datetime.now(tz=_TIMEZONE_NEW_YORK), id='unknown')
        )

    def __str__(self):
        return f"{self.revision} ERROR: {super().__str__()}"


class EffectiveDate(dt.date):
    circa: bool = False

    @staticmethod
    def from_date(date: dt.date, circa=False) -> 'EffectiveDate':
        return EffectiveDate(
            year=date.year,
            month=date.month,
            day=date.day,
            circa=circa
        )

    def to_date(self) -> dt.date:
        return dt.date(self.year, self.month, self.day)

    def __eq__(self, other):
        return super().__eq__(other) and self.circa == other.circa

    def __ne__(self, other):
        return not self.__eq__(other)

    def __new__(cls, year, month=None, day=None, circa=False):
        self = super().__new__(cls, year=year, month=month, day=day)
        self.circa = circa
        return self

    def __str__(self):
        return f"{self.strftime(_YYYY_MM_DD)}{'*' if self.circa else ''}"


@dataclass
class Stock:
    date_added: EffectiveDate | None = None
    date_removed: EffectiveDate | None = None
    created_at: dt.date | None = None

    symbol: str = ''
    name: str = ''
    sector: str = ''
    cik: str = ''

    def __eq__(self, other):
        return (
            self.symbol,
            self.cik,
            self.date_removed,
            self.date_added,
            self.sector,
            self.name
        ) == (
            other.symbol,
            other.cik,
            other.date_removed,
            other.date_added,
            other.sector,
            other.name
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __lt__(self, other):
        return (
            self.symbol,
            self.cik,
            self.date_removed if self.date_removed else _DATE_MAX,
            self.date_added if self.date_added else _DATE_MIN,
            self.sector,
            self.name
        ) < (
            other.symbol,
            other.cik,
            other.date_removed if other.date_removed else _DATE_MAX,
            other.date_added if other.date_added else _DATE_MIN,
            other.sector,
            other.name
        )

    def complete(self) -> bool:
        return bool(self.symbol and self.name and self.sector and self.cik and self.date_added)

    def to_list(self, include_dates: bool = False) -> list[str]:
        return list(self.to_dict(include_dates).values())

    def to_dict(self, include_dates: bool = False) -> dict[str, str]:
        base = {
            'symbol': self.symbol,
            'cik': self.cik,
            'name': self.name,
            'sector': self.sector,
        }
        if include_dates:
            base['date_added'] = str(self.date_added) if self.date_added else ''
            base['date_removed'] = str(self.date_removed) if self.date_removed else ''
            base['created_at'] = str(self.created_at) if self.created_at else ''
        return base


@dataclass
class Changeset:
    revision: Revision
    added: list[Stock] = field(default_factory=list)
    removed: list[Stock] = field(default_factory=list)
    updated: list[Stock] = field(default_factory=list)
    unchanged: list[Stock] = field(default_factory=list)
    inactive: list[Stock] = field(default_factory=list)
    inactive_updated: list[Stock] = field(default_factory=list)

    def summary(self) -> str:
        added = [s.symbol for s in self.added]
        removed = [s.symbol for s in self.removed]
        updated = [s.symbol for s in self.updated + self.inactive_updated]
        if added or removed or updated:
            added_msg = f" +{','.join(added)}" if added else ''
            removed_msg = f" -{','.join(removed)}" if removed else ''
            updated_msg = f" *{','.join(sorted(updated))}" if updated else ''
            return f"{self.revision}{added_msg}{removed_msg}{updated_msg}"
        return ''


@dataclass
class _RemovalHistory:
    date_removed: EffectiveDate | None = None
    symbol: str = ''

    def __lt__(self, other):
        # NOTE: Reverse chronological order!
        return (self.date_removed or _DATE_MAX) > (other.date_removed or _DATE_MAX)

    def complete(self) -> bool:
        return bool(self.symbol and self.date_removed)


class _StockField(StrEnum):
    SYMBOL = 'symbol'
    CIK = 'cik'
    NAME = 'name'
    SECTOR = 'sector'
    DATE_ADDED = 'date_added'
    DATE_REMOVED = 'date_removed'
    CREATED_AT = 'created_at'


class _RemovalHistoryField(StrEnum):
    SYMBOL = 'symbol'
    DATE_REMOVED = 'date_removed'


class _Sector(StrEnum):
    ENERGY = 'energy'
    MATERIALS = 'materials'
    INDUSTRIALS = 'industrials'
    CONSUMER_DISCRETIONARY = 'consumer_discretionary'
    CONSUMER_STAPLES = 'consumer_staples'
    HEALTH_CARE = 'health_care'
    FINANCIALS = 'financials'
    INFORMATION_TECHNOLOGY = 'information_technology'
    COMMUNICATION_SERVICES = 'communication_services'
    UTILITIES = 'utilities'
    REAL_ESTATE = 'real_estate'


class _Month(Enum):
    JANUARY = 1
    FEBRUARY = 2
    MARCH = 3
    APRIL = 4
    MAY = 5
    JUNE = 6
    JULY = 7
    AUGUST = 8
    SEPTEMBER = 9
    OCTOBER = 10
    NOVEMBER = 11
    DECEMBER = 12


def _reverse_dict(mapping: dict[T, list[str]]) -> dict[str, T]:
    out: dict[str, T] = {}
    for key, matches in mapping.items():
        for text in matches:
            out[text] = key
    return out


_MATCH_STOCK_FIELD = _reverse_dict({
    _StockField.SYMBOL: ['symbol', 'ticker', 'ticker_symbol'],
    _StockField.NAME: ['security', 'company', 'name', 'company_name'],
    _StockField.SECTOR: ['gics_sector', 'sector', 'industry'],
    _StockField.CIK: ['cik', 'cik_number', 'central_index_key'],
    _StockField.DATE_ADDED: ['date_added', 'added', 'date_first_added'],
    _StockField.DATE_REMOVED: ['date_removed', 'removed'],
    _StockField.CREATED_AT: ['created_at'],
})


_MATCH_REMOVAL_HISTORY_FIELD = _reverse_dict({
    _RemovalHistoryField.SYMBOL: ['removed_ticker', 'removed_symbol'],
    _RemovalHistoryField.DATE_REMOVED: ['date'],
})


# https://en.wikipedia.org/wiki/Global_Industry_Classification_Standard
_MATCH_SECTOR = _reverse_dict({
    _Sector.ENERGY: [
        'energy',
        'oil_&_gas_storage_&_transportation'
    ],
    _Sector.MATERIALS: [
        'materials',
        'diversified_metals_&_mining',
        'paper_packaging'
    ],
    _Sector.INDUSTRIALS: [
        'industrials',
        'building_products',
        'data_processing_&_outsourced_services'
    ],
    _Sector.CONSUMER_DISCRETIONARY: [
        'consumer_discretionary',
        'department_stores',
        'hotels_resorts_&_cruise_lines'
    ],
    _Sector.CONSUMER_STAPLES: [
        'consumer_staples',
        'general_merchandise_stores'
    ],
    _Sector.HEALTH_CARE: [
        'health_care',
        'biotechnology',
        'health_care_equipment',
        'health_care_facilities',
        'health_care_services',
        'health_care_supplies'
    ],
    _Sector.FINANCIALS: [
        'financials',
        'asset_management_&_custody_banks',
        'regional_banks'
    ],
    _Sector.INFORMATION_TECHNOLOGY: [
        'information_technology',
        'communications_equipment',
        'computer_hardware',
        'electronic_equipment_manufacturers',
        'electronic_manufacturing_services',
        'semiconductors'
    ],
    _Sector.COMMUNICATION_SERVICES: [
        'broadcasting_&_cable_tv',
        'communication_services',
        'publishing',
        'telecommunications_services',
        'telecommunication_services',
        'wireless_telecommunication_services'
    ],
    _Sector.UTILITIES: [
        'utilities',
        'independent_power_producers_&_energy_traders',
        'multi_utilities'
    ],
    _Sector.REAL_ESTATE: [
        'real_estate',
        'real_estate_management_&_development',
        'residential_reits'
    ],
})


_MATCH_MONTH = _reverse_dict({
    _Month.JANUARY: ['january', 'jan'],
    _Month.FEBRUARY: ['february', 'feb'],
    _Month.MARCH: ['march', 'mar', 'mr'],
    _Month.APRIL: ['april', 'apr'],
    _Month.MAY: ['may', 'ma'],
    _Month.JUNE: ['june', 'jun'],
    _Month.JULY: ['july', 'jul'],
    _Month.AUGUST: ['august', 'aug'],
    _Month.SEPTEMBER: ['september', 'sept', 'sep'],
    _Month.OCTOBER: ['october', 'oct'],
    _Month.NOVEMBER: ['november', 'nov'],
    _Month.DECEMBER: ['december', 'dec']
})


# Special handling for hotly disputed symbols
_SYMBOL_ALIAS = _reverse_dict({
    'BAX': ['BAX.B'],
    'BF.B': ['BF', 'BF-A', 'BF-B', 'BF/B', 'BF_B', 'BFB'],
    'BRK.B': ['BRK', 'BRK.A', 'BRK-A', 'BRK-B', 'BRK/A', 'BRK/B', 'BRK_A', 'BRK_B', 'BRKB'],
    'NWSA': ['NWS.A'],
    'UA.C': ['UA-C', 'UA/C', 'UA_C'],
    'VIAB': ['VIA.B'],
    'SPG': ['SPG.PJ'],
    'WPX': ['WPX.WI', 'WPX-WI'],
})


#########################################################
# utils
#########################################################


def project_root() -> Path:
    return Path(__file__).parent.parent


def _day_month_year_to_date(s: str) -> dt.date | None:
    # input format: 31 December 1999
    s = ' '.join(s.split()).replace('.', '').replace(',', '')
    parts = s.split(' ')
    if len(parts) != 3:
        return None
    month = _MATCH_MONTH.get(parts[1].lower())
    if not month:
        return None
    try:
        date = dt.date(year=int(parts[2]), month=month.value, day=int(parts[0]))
    except ValueError:
        return None
    return date


def _extract_revision(page_html: str) -> Revision | None:
    # This can also be queried with Wikimedia API but would incur another request

    # Find timestamp
    timestamp_search = re.search(_RE_PAGE_TIMESTAMP, page_html)
    if not timestamp_search:
        return None
    groups = timestamp_search.groups()
    if len(groups) != 2:
        return None
    time_parts = groups[1].split(':')
    if len(time_parts) != 2:
        return None
    date = _day_month_year_to_date(groups[0])
    if not date:
        return None
    try:
        timestamp = dt.datetime(
            year=date.year,
            month=date.month,
            day=date.day,
            hour=int(time_parts[0]),
            minute=int(time_parts[1]),
            tzinfo=dt.timezone.utc
        )
    except ValueError:
        return None

    # Find revision id
    revision_search = re.search(_RE_REVISION_ID, page_html)
    if not revision_search:
        return None
    return Revision(
        timestamp=timestamp,
        id=revision_search.group(1)
    )


def _is_table_fixable(table: Table) -> bool:
    max_width = table.max_width()
    min_width = max_width
    for row in table:
        min_width = min(min_width, len(row.cells))
    delta = max_width - min_width
    return min_width > 0 and delta <= 1


#########################################################
# input/output
#########################################################


def read_file(file_path: Path, encoding: str = 'utf-8') -> str:
    try:
        with file_path.open(mode='r', encoding=encoding) as file:
            return file.read()
    except IOError:
        return ''


def read_last_line(file_path: Path, encoding: str = 'utf-8') -> str:
    lines = read_file(file_path, encoding).splitlines()
    return lines[-1] if lines else ''


def read_components_history(
    components_history_file: Path
) -> list[Stock]:
    return _csv_to_stocks(read_file(components_history_file))


def write_file(file_path: Path, content: str, mode: str = 'w') -> None:
    append = mode == 'a'
    with file_path.open(mode='a' if append else 'w', encoding='utf-8', newline='') as output:
        output.write(f"{content}\n")


def write_components_history(
    components_history_file: Path,
    components_history: list[Stock]
) -> None:
    write_replace_csv(components_history_file, components_history, True)


def write_replace_csv(file_path: Path, stocks: list[Stock], include_dates: bool = False) -> None:
    file_path.unlink(missing_ok=True)
    with file_path.open(mode='w', encoding='utf-8', newline='') as output:
        writer = csv.writer(
            output,
            delimiter=',',
            quotechar='"',
            escapechar=None,
            doublequote=True,
            skipinitialspace=False,
            lineterminator='\n',
            quoting=csv.QUOTE_MINIMAL,
        )
        date_fields = set([_StockField.DATE_ADDED, _StockField.DATE_REMOVED, _StockField.CREATED_AT])
        writer.writerow(f for f in _StockField if include_dates or f not in date_fields)
        writer.writerows(stock.to_list(include_dates) for stock in stocks)


def write_replace_json(file_path: Path, stocks: list[Stock], include_dates: bool = False) -> None:
    file_path.unlink(missing_ok=True)
    with file_path.open(mode='w', encoding='utf-8', newline='') as output:
        output.write(json.dumps({'components': [stock.to_dict(include_dates) for stock in stocks]}))


def request_http(url: str, encoding: str = 'utf-8', request_headers: dict[str, str] | None = None) -> str:
    headers = dict(request_headers or {})
    headers.update({'User-Agent': _USER_AGENT})
    try:
        with urlopen(Request(url=url, headers=headers)) as resp:
            return resp.read().decode(encoding)
    except Exception as e:
        raise IOError(f"Failed to make HTTP request. Error:{repr(e)} Url: {url} Headers: {str(headers)}") from None


def _fetch_tables(data_source: str = '') -> tuple[Table, Table, Revision]:
    retries = 3
    data_source = data_source.strip() or _PAGE_URL_BASE
    page_html = data_source
    while (data_source.startswith('http://') or data_source.startswith('https://')) and retries > 0:
        try:
            page_html = request_http(data_source)
            break
        except (HTTPError, IOError) as e:
            retries -= 1
            if retries <= 0:
                raise e
            time.sleep(1)
    revision = _extract_revision(page_html)
    if not revision:
        raise ValueError('Failed to extract revision information from data source')
    try:
        tables = parse_html(page_html, match=_RE_EXTRACT_TABLES)
    except ValueError as e:
        raise UpdateError(f"Parse HTML error - {str(e)}", revision=revision) from None
    if not tables:
        raise UpdateError('No tables found', revision=revision)
    components_table = tables[0]
    removal_history_table = Table()
    if len(tables) > 1:
        removal_history_table = tables[1]
        if not removal_history_table.is_rectangular():
            if _is_table_fixable(removal_history_table):
                removal_history_table.rectangify()
            else:
                raise UpdateError('Removal history table is ragged', revision=revision)
    if not components_table.is_rectangular():
        if _is_table_fixable(components_table):
            components_table.rectangify()
        else:
            raise UpdateError('Components table is ragged', revision=revision)
    return components_table, removal_history_table, revision


def _fetch_page(revision_id: str, cacheless: bool = False):
    page_path = project_root() / 'pages' / f"{revision_id}.html"
    if not cacheless:
        cached_page = read_file(page_path)
        if cached_page:
            print(f"Processing page revision {revision_id} from cache: {page_path}")
            return cached_page
    time.sleep(1)
    print(f"Processing page revision {revision_id}")
    result = request_http(_PAGE_URL_BASE + revision_id)
    if not cacheless:
        page_path.parent.mkdir(exist_ok=True)
        write_file(page_path, result)
    return result


#########################################################
# converters
#########################################################


def _collapse_whitespace(s: str) -> str:
    return ' '.join(s.split())


def _to_token(s: str) -> str:
    return '_'.join(s.replace('-', ' ').replace(',', '').split()).lower()


def _to_symbol(s: str) -> str:
    # In case prefixed by exchange
    parts = s.split(':')
    if len(parts) > 1:
        s = parts[1]
    s = s.strip()
    if re.match(_RE_SYMBOL, s):
        return _SYMBOL_ALIAS.get(s, s)
    return ''


def _to_sector(s: str) -> str:
    sector = _MATCH_SECTOR.get(_to_token(s))
    if sector:
        return sector.value
    return ''


def _to_cik(s: str) -> str:
    s = ''.join(s.split())
    try:
        num = int(s)
    except ValueError:
        return ''
    # up to 10 digits
    if 0 < num < 10_000_000_000:
        return s.zfill(10)
    return ''


def _to_date(s: str) -> dt.date | None:
    return _iso8601_to_date(s) or _english_to_date(s)


def _to_effective_date(s: str) -> EffectiveDate | None:
    circa = s.endswith('*')
    if circa:
        s = s[:-1]
    date = _to_date(s)
    if not date:
        return None
    return EffectiveDate.from_date(date=date, circa=circa)


def _iso8601_to_date(s: str) -> dt.date | None:
    # input format: 1999-12-31
    s = ''.join(s.split())
    parts = s.split('-')
    if len(parts) != 3:
        return None
    try:
        date = dt.date(year=int(parts[0]), month=int(parts[1]), day=int(parts[2]))
    except ValueError:
        return None
    return date


def _english_to_date(s: str) -> dt.date | None:
    # input format: December 31, 1999
    # NOTE: Cannot use strptime because of dependency on locale
    s = ' '.join(s.split()).replace('.', '').replace(',', '')
    parts = s.split(' ')
    if len(parts) != 3:
        return None
    month = _MATCH_MONTH.get(parts[0].lower())
    if not month:
        return None
    try:
        date = dt.date(year=int(parts[2]), month=month.value, day=int(parts[1]))
    except ValueError:
        return None
    return date


#########################################################
# parsers
#########################################################


def _csv_to_stocks(text: str, revision: Revision | None = None) -> list[Stock]:
    reader = csv.reader(
        io.StringIO(text),
        delimiter=',',
        quotechar='"',
        escapechar=None,
        doublequote=True,
        skipinitialspace=False,
        lineterminator='\n',
        quoting=csv.QUOTE_MINIMAL,
    )

    # Map headers to fields
    try:
        header_row = next(reader)
    except StopIteration:
        return []
    field_headings = [_MATCH_STOCK_FIELD.get(_to_token(heading)) for heading in header_row]

    found = set(f.value for f in field_headings if f)
    required = set([_StockField.SYMBOL.value, _StockField.NAME.value, _StockField.SECTOR.value])
    if (found & required) != required:
        raise UpdateError(
            f"Symbol, name or sector headings missing. Found: {','.join(sorted(found))}",
            revision=revision
        )

    # Convert each row to Stock
    stocks: list[Stock] = []
    row_count = 0
    for row in reader:
        stock = Stock()
        for idx, value in enumerate(row):
            match field_headings[idx]:
                case _StockField.SYMBOL:
                    stock.symbol = _to_symbol(value)
                case _StockField.NAME:
                    stock.name = _collapse_whitespace(value)
                case _StockField.SECTOR:
                    stock.sector = _to_sector(value)
                case _StockField.CIK:
                    stock.cik = _to_cik(value)
                case _StockField.DATE_ADDED:
                    stock.date_added = _to_effective_date(value)
                case _StockField.DATE_REMOVED:
                    stock.date_removed = _to_effective_date(value)
                case _StockField.CREATED_AT:
                    stock.created_at = _to_date(value)
                case _:
                    pass
        if stock.symbol:
            stocks.append(stock)
        row_count += 1
    if len(stocks) < row_count:
        raise UpdateError(f"Found {row_count - len(stocks)} rows without symbols", revision=revision)
    stocks.sort()
    return stocks


def _table_to_removal_history(table: Table, revision: Revision) -> dict[str, list[_RemovalHistory]]:
    if len(table.rows) < 2:
        return {}

    # Map headers to fields
    field_headings: list[_RemovalHistoryField | None] = []
    for level_one, level_two in zip(table.rows[0].cells, table.rows[1].cells):
        heading_one = _collapse_whitespace(level_one.inner_text())
        heading_two = _collapse_whitespace(level_two.inner_text())
        # Merge level one and two headings together
        heading = _to_token(heading_one if heading_one == heading_two else f"{heading_one} {heading_two}")
        field_headings.append(_MATCH_REMOVAL_HISTORY_FIELD.get(heading))

    found = set(f.value for f in field_headings if f)
    required = set([_RemovalHistoryField.SYMBOL.value, _RemovalHistoryField.DATE_REMOVED.value])
    if (found & required) != required:
        raise UpdateError(
            f"Symbol or removal date headings missing. Found: {','.join(sorted(found))}",
            revision=revision
        )

    history_lookup: dict[str, list[_RemovalHistory]] = {}
    for row in table.rows[2:]:
        entry = _RemovalHistory()
        for idx, cell in enumerate(row):
            value = cell.inner_text()
            match field_headings[idx]:
                case _RemovalHistoryField.SYMBOL:
                    entry.symbol = _to_symbol(value)
                case _RemovalHistoryField.DATE_REMOVED:
                    entry.date_removed = _to_effective_date(value)
                case _:
                    pass
        if entry.complete():
            dates_removed = history_lookup.get(entry.symbol, [])
            heapq.heappush(dates_removed, entry)
            history_lookup[entry.symbol] = dates_removed
    return history_lookup


#########################################################
# logic
#########################################################


def _merge_effective_date(old_date: EffectiveDate, latest_date: EffectiveDate) -> EffectiveDate:
    if not latest_date.circa:
        return latest_date
    elif not old_date.circa:
        return old_date
    elif latest_date - old_date < dt.timedelta(days=30):
        return latest_date
    else:
        return old_date


def _merge_stocks(old: Stock, latest: Stock):
    out = Stock()
    for f in fields(Stock):
        setattr(out, f.name, getattr(latest, f.name) or getattr(old, f.name))
    if old.date_added and latest.date_added:
        out.date_added = _merge_effective_date(old.date_added, latest.date_added)
    if old.date_removed and latest.date_removed:
        out.date_removed = _merge_effective_date(old.date_removed, latest.date_removed)
    if old.created_at and latest.created_at:
        out.created_at = old.created_at
    return out


def _find_duplicates(items: list[T]) -> set[T]:
    seen = set()
    dupes = set()
    for item in items:
        if item in seen:
            dupes.add(item)
        else:
            seen.add(item)
    return dupes


def _find_closest_removal_date(removals: list[_RemovalHistory], date_ref: EffectiveDate) -> EffectiveDate | None:
    # linear search does not assume sorted removals
    closest_date = None
    diff_min = float('inf')
    for history in removals:
        if not history.date_removed:
            continue
        diff = abs((history.date_removed - date_ref).total_seconds())
        if diff < diff_min:
            closest_date = history.date_removed
            diff_min = diff
    return closest_date


def _get_date_removed(
    stock: Stock,
    removals: dict[str, list[_RemovalHistory]],
    date_stand_in: EffectiveDate
) -> EffectiveDate | None:
    history = removals.get(stock.symbol)
    closest_date_removed = _find_closest_removal_date(history, date_stand_in) if history else None
    if (
        closest_date_removed
        and stock.date_added
        and closest_date_removed >= stock.date_added
    ):
        return closest_date_removed
    if stock.date_added and stock.date_added > date_stand_in:
        return EffectiveDate.from_date(stock.date_added, circa=True)
    return date_stand_in


def _create_backfill_lookup(stocks: list[Stock]) -> dict[str, list[Stock]]:
    lookup: dict[str, list[Stock]] = {}
    for s in stocks:
        entries = lookup.get(s.symbol, [])
        entries.append(s)
        lookup[s.symbol] = entries
    return lookup


_BACKFILLS = _create_backfill_lookup(read_components_history(Path(__file__).parent / 'backfill.csv'))


def _backfill(stock: Stock, omit_removal: bool = False) -> Stock:
    for b in _BACKFILLS.get(stock.symbol, []):
        if (b
            and b.date_added
            and b.date_removed
            and stock.date_added
            and b.date_added <= stock.date_added < b.date_removed
        ):
            merged = _merge_stocks(stock, b)
            return replace(merged, date_removed=None) if omit_removal else merged
    return stock


def _diff_lists(
    components_history: list[Stock],
    latest: list[Stock],
    revision: Revision,
    removals: dict[str, list[_RemovalHistory]]
) -> Changeset:
    # NOTE: Assumes both lists are sorted by symbols already!

    # Filter out inactive stocks as we only want to diff active stocks
    old: list[Stock] = []
    inactive: list[Stock] = []
    inactive_updated: list[Stock] = []

    # Iterate in reverse so latest entry considered first
    seen = set()
    for existing in reversed(components_history):
        if not existing.date_removed:
            # stocks in index
            old.append(existing)
        else:
            # stocks removed from index - check if removal date updated
            updated_date_removed = _get_date_removed(existing, removals, existing.date_removed)
            if (
                existing.symbol not in seen # only try to update latest symbol if multiple entries
                and updated_date_removed
                and existing.date_removed != updated_date_removed
            ):
                inactive_updated.append(replace(existing, date_removed=updated_date_removed))
            else:
                inactive.append(existing)
        seen.add(existing.symbol)
    old.reverse()
    inactive.reverse()
    inactive_updated.reverse()

    old_dupes = _find_duplicates([s.symbol for s in old])
    latest_dupes = _find_duplicates([s.symbol for s in latest])
    if old_dupes:
        raise UpdateError(f"Duplicate symbol(s) in old stocks list: {','.join(old_dupes)}", revision=revision)
    if latest_dupes:
        raise UpdateError(f"Duplicate symbol(s) in latest stocks list: {','.join(latest_dupes)}", revision=revision)

    idx_old = 0
    idx_latest = 0

    added: list[Stock] = []
    removed: list[Stock] = []
    updated: list[Stock] = []
    unchanged: list[Stock] = []

    date = revision.ny_date()
    date_stand_in = EffectiveDate.from_date(date=date, circa=True)

    while idx_old < len(old) and idx_latest < len(latest):
        stock_old = old[idx_old]
        stock_latest = latest[idx_latest]

        if stock_old.symbol < stock_latest.symbol:
            # Removals
            stock_removed = replace(stock_old, date_removed=_get_date_removed(stock_old, removals, date_stand_in))
            removed.append(_backfill(stock_removed))
            idx_old += 1
            continue
        if stock_latest.symbol < stock_old.symbol:
            # Additions
            stock_added = replace(stock_latest, date_added=stock_latest.date_added or date_stand_in, created_at=date)
            added.append(_backfill(stock_added, True))
            idx_latest += 1
            continue

        stock_merged = _merge_stocks(stock_old, stock_latest)
        if stock_merged != stock_old:
            updated.append(stock_merged)
        else:
            unchanged.append(stock_old)

        idx_old += 1
        idx_latest += 1

    while idx_old < len(old):
        # Removals
        stock_old = old[idx_old]
        stock_removed = replace(stock_old, date_removed=_get_date_removed(stock_old, removals, date_stand_in))
        removed.append(_backfill(stock_removed))
        idx_old += 1

    while idx_latest < len(latest):
        # Additions
        stock_latest = latest[idx_latest]
        stock_added = replace(stock_latest, date_added=stock_latest.date_added or date_stand_in, created_at=date)
        added.append(_backfill(stock_added, True))
        idx_latest += 1

    return Changeset(
        revision,
        added=added,
        removed=removed,
        updated=updated,
        unchanged=unchanged,
        inactive=inactive,
        inactive_updated=inactive_updated
    )


def _create_components_history(changeset: Changeset) -> list[Stock]:
    # Create map for faster lookup
    added_map: dict[str, Stock] = {}
    for s in changeset.added:
        added_map[s.symbol] = s

    # If added stock was recently removed, it is probably a glitch
    # Cancel existing removal instead of adding a new entry
    added_exclusion = set()
    merged_components_prev: list[Stock] = []

    # Iterate in reverse so latest entry considered first
    for existing in reversed(sorted(changeset.inactive + changeset.inactive_updated)):
        added = added_map.get(existing.symbol)
        if (
            added
            and added.symbol not in added_exclusion
            and existing.date_removed
            and added.date_added
            and (added.date_added - existing.date_removed) < dt.timedelta(days=30)
        ):
            merged = _merge_stocks(existing, added)
            merged.date_removed = None # undo removal
            merged_components_prev.append(merged)
            added_exclusion.add(added.symbol)
        else:
            merged_components_prev.append(existing)
    added_new = [s for s in changeset.added if s.symbol not in added_exclusion]

    # If inclusion duration is too short, it is probably a glitch
    # Exclude it from history
    removed_new = [
        s for s in changeset.removed
        if (s.date_removed and s.date_added and (s.date_removed - s.date_added) > dt.timedelta(days=14))
        and (s.created_at and (changeset.revision.ny_date() - s.created_at) > dt.timedelta(days=2))
    ]

    components_history_new = [
        *added_new,
        *removed_new,
        *changeset.updated,
        *changeset.unchanged,
        *merged_components_prev
    ]
    components_history_new.sort()
    return components_history_new


def _fetch_revision_ids(newer_than_revision_id: str) -> list[str]:
    response = request_http(_REVISION_API_BASE + newer_than_revision_id)
    obj = json.loads(response)
    # Revisions from API are returned latest first, so reverse for chronological order
    return [str(r['id']) for r in reversed(obj['revisions'])]


def _last_processed_revision_id(changelog_file: Path) -> str:
    parts = read_last_line(changelog_file).split(' ')
    if len(parts) > 1:
        try:
            revision_num = int(parts[1].replace('(', '').replace(')', ''))
        except ValueError as _e:
            raise ValueError('Failed to parse revision from changelog') from None
        return str(revision_num)
    return ''


def _update_components_history(
    components_history: list[Stock],
    data_source: str = ''
) -> Changeset:
    # New list
    components_table, removals_table, revision = _fetch_tables(data_source)
    components_latest = _csv_to_stocks(components_table.to_csv(), revision=revision)

    # Calculate new list
    removals = _table_to_removal_history(removals_table, revision)
    changeset = _diff_lists(components_history, components_latest, revision, removals)
    return changeset


def _update_components_history_file(
    components_history_file: Path,
    changelog_file: Path,
    step_mode: bool = False,
    cacheless: bool = False
) -> None:
    now = dt.datetime.now()
    starting_from_revision_id = _last_processed_revision_id(changelog_file) or _FIRST_REVISION_WITH_SYMBOLS
    revisions = [starting_from_revision_id]
    components_history = read_components_history(components_history_file)
    while revisions:
        for revision_id in revisions:
            page_html = _fetch_page(revision_id, cacheless)
            try:
                changeset = _update_components_history(components_history, page_html)
            except UpdateError as e:
                # Update encountered error
                message = str(e)
                if read_last_line(changelog_file) != message:
                    write_file(changelog_file, message, mode='a')
                continue
            message = changeset.summary()
            if message:
                components_history = _create_components_history(changeset)
                write_file(changelog_file, message, mode='a')
                write_components_history(components_history_file,components_history)
            if step_mode:
                key = input('Press any key to continue or "q" to quit...')
                if key.strip().lower() == 'q':
                    revisions = []
                    break
        if revisions:
            revisions = _fetch_revision_ids(revisions[-1])
    print(f"Update complete in {round((dt.datetime.now() - now).total_seconds())}s")


def list_components(components_history: list[Stock], date: dt.date | None = None) -> list[Stock]:
    if date is None:
        return [c for c in components_history if not c.date_removed]
    # The following comments are CRITICAL for understanding the code. Do not omit them:
    # We must use DATE_MAX=3000-01-01 as the date removed if not provided or the code won't work at all.
    # This is because the date must be set sufficiently in the future to always include the component.
    # Ignore suggestions that the value can be changed to something else.
    components = [
        c for c in components_history
        if (c.date_added.to_date() if c.date_added else _DATE_MIN)
        <= date < (c.date_removed.to_date() if c.date_removed else _DATE_MAX)
        and date >= (c.created_at or _DATE_MIN)
    ]
    return components


def update(
    data_folder: Path,
    data_delayed_folder: Path,
    step_mode: bool = False,
    cacheless: bool = False
) -> None:
    data_folder.mkdir(parents=True, exist_ok=True)
    data_delayed_folder.mkdir(parents=True, exist_ok=True)

    components_history_file = data_folder / COMPONENTS_HISTORY_FILE_NAME
    changelog_file = data_folder / CHANGELOG_FILE_NAME

    _update_components_history_file(
        components_history_file,
        changelog_file,
        step_mode,
        cacheless
    )

    components_history_new = read_components_history(components_history_file)

    if components_history_new:
        # Export latest data
        csv_file = data_folder / CSV_FILE_NAME
        json_file = data_folder / JSON_FILE_NAME
        now = dt.datetime.now(tz=_TIMEZONE_NEW_YORK).date()
        components_new = list_components(components_history_new, now)
        write_replace_csv(csv_file, components_new)
        write_replace_json(json_file, components_new)

        # Export delayed data
        csv_file_delayed = data_delayed_folder / CSV_FILE_NAME
        json_file_delayed = data_delayed_folder / JSON_FILE_NAME
        two_days_ago = now - dt.timedelta(days=2)
        components_delayed = list_components(components_history_new, two_days_ago)
        write_replace_csv(csv_file_delayed, components_delayed)
        write_replace_json(json_file_delayed, components_delayed)


#########################################################
# main
#########################################################


if __name__ == '__main__':
    update(
        data_folder=project_root() / 'data',
        data_delayed_folder=project_root() / 'data_delayed',
        step_mode=False,
        cacheless=len(sys.argv) > 1
    )
