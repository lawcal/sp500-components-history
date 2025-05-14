# S&P 500 Components History

[![Update](https://github.com/lawcal/sp500-components-history/actions/workflows/update.yml/badge.svg?branch=main)](https://github.com/lawcal/sp500-components-history/actions/workflows/update.yml) | [View Latest Update](https://github.com/lawcal/sp500-components-history/compare/main~1...main)

Current and historical S&P 500 companies list since March 5, 2007. Data is checked daily from Wikipedia's [list of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies).

The following fields are available for each company that is or was part of the S&P 500:

| Field  | Description       | Format                    |
| ------ | ----------------- | ------------------------- |
| symbol | Ticker symbol     | Uppercase string          |
| cik    | Central Index Key | 10-digit number as string |
| name   | Company name      | Freeform string           |
| sector | GICS Sector       | `energy`<br/>`materials`<br/>`industrials`<br/>`consumer_discretionary`<br/>`consumer_staples`<br/>`health_care`<br/>`financials`<br/>`information_technology`<br/>`communication_services`<br/>`utilities`<br/>`real_estate` |

Because the data source only [started tracking CIK in 2014](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies?oldid=607709431), missing CIKs were manually backfilled by referencing the following resources:
- [EDGAR Company Database](https://www.edgarcompany.sec.gov/)
- [QuantumOnline Quick Search](https://www.quantumonline.com/search.cfm)

## Usage

The current and two-day-delayed components lists are found in `/data` and `/data_delayed`, respectively. They are available as JSON and CSV.

Both lists are derived from the latest `components_history.csv`, which is an effective date table. The delayed list is generated with a past date to insulate against errors in the latest revision. See [Data Source Caveats](#data-source-caveats) for more information.

### JSON
GET `https://github.com/lawcal/sp500-components-history/raw/main/data/sp500_components.json`

```
{
    "components": [
        {
            "symbol": "A",
            "cik": "0001090872"
            "name": "Agilent Technologies",
            "sector": "health_care",
        },
        ...
    ]
}
```

### CSV
GET `https://github.com/lawcal/sp500-components-history/raw/main/data/sp500_components.csv`

```
symbol,cik,name,sector
A,0001090872,Agilent Technologies,health_care
...
```

## Get Historical Components

Historical components can be retrieved by processing `components_history.csv` using the `date_added`, `date_removed` and `created_at` columns for filtering. Alternatively, use the `list_components()` helper function.

First, clone the repository and install dependencies:
```
pip install -r requirements.txt
```

Then run the following snippet:
```
import datetime as dt
from pathlib import Path
from bin.update import list_components, read_components_history, write_replace_csv

components_history_csv = Path('data/components_history.csv')
output_csv = Path('output.csv')
historical_date = dt.date(2020, 1, 1)

historical_components = list_components(
    read_components_history(components_history_csv),
    historical_date
)

# (Optional) Export components to CSV
write_replace_csv(output_csv, historical_components)

```

## Data Source Caveats

The `components_history.csv` file is constructed by parsing through every revision of the Wikipedia page while tracking additions, removals and field changes. While there are built-in validations and filtering strategies to keep the data clean, the quality is only as good as the source.

Below are challenges posed by the data source and mitigations:

1. Components tracking with symbols [started on March 5, 2007](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=112958830).
    - Do not query for historical components before this date.
2. Addition date tracking [started on August 18, 2012](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=507960752).
    - If a symbol's addition or removal date cannot be found, the revision timestamp converted to local New York time is used as an approximation.
3. Company and symbol values are swapped or invalid. Example: [swapped headers](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=185113306), [invalid symbols](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=233849613)
    - Revision is skipped if there are any rows with symbols that cannot be parsed.
4. Duplicate entry is found in table. Example: [duplicate AIZ symbol](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=230711443)
    - Revision is skipped if duplicate symbols are found.
5. Table is malformed or missing. Example: [missing table tag](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=389847709), [vandalism](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=427992852)
    - Revision is skipped if table does not contain certain required headings.
6. Table formatting is broken. Example: [wide row due to unclosed tag](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=421482407)
    - Revision is skipped if the table is ragged.
7. Multiple symbol aliases due to various conventions. Example: [BRK-B](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=985987275)
    - Include a mapping of various aliases for the symbol and settle on one convention. Using the above example, it resolves to BRK.B.
8. Incorrect symbol is added. Example: [SPOT mistakenly added](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=964768725)
    - Typically the error would be spotted and removed in a later revision.
    - If the symbol is removed shortly after, it will be omitted from the components history.
9. Symbol is added before the actual effective date. Example: [TSLA added early](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=989167414)
    - Typically the error would be spotted and removed in a later revision, then added on the correct date.
    - If the early symbol is removed shortly after, it will be omitted from the components history.
10. Incorrect symbol is assigned to company. Example: [QQQ for 3M](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=555200632)
    - If the symbol is removed shortly after, it will be omitted from the components history.
    - Furthermore, if a symbol is reinstated after a short time compared to the latest removal date, the old entry is restored with the removal date cleared.
11. Company changes ticker symbol. Example: [FB to META](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=1092243288)
    - All new symbols when first recorded are assigned a creation date set to the revision date. This facilitates filtering out the new symbol if the change hasn't occurred on a historical date.
    - This is a best effort approach as it's possible for the old and new symbol's date ranges to overlap.
    - Except for symbol aliases, the system treats each unique symbol as an independent entry. It does not have the concept of symbol changes.

## Developing

Install development dependencies:
```
pip install -r requirements.txt mypy
```

To update your local data to the latest revision, run:
```
python bin/update.py
```

## Testing Instructions

Test code changes by deleting the `/data` and `/data_delayed` folders and running `python bin/update.py` to recreate the components history from scratch. Compare the generated files against their old ones.
