# S&P 500 Components History

Current and historical S&P 500 companies list since May 5, 2007. Data is checked daily from Wikipedia's [list of S&P 500 companies](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies).

The following fields are available for each company that is or was part of the S&P 500:

| Field  | Description       | Format                    |
| ------ | ----------------- | ------------------------- |
| symbol | Ticker symbol     | Uppercase string          |
| cik    | Central Index Key | 10-digit number as string |
| name   | Company name      | Freeform string           |
| sector | GICS Sector       | `energy`<br/>`materials`<br/>`industrials`<br/>`consumer_discretionary`<br/>`consumer_staples`<br/>`health_care`<br/>`financials`<br/>`information_technology`<br/>`communication_services`<br/>`utilities`<br/>`real_estate` |

Note: CIK numbers may be missing for certain companies before May 1, 2014. This is because the data source [started tracking them in 2014](https://en.wikipedia.org/wiki/List_of_S%26P_500_companies?oldid=607709431).

## Usage

The current and two-day-delayed components list can be found in `/data` and `/data_delayed`, respectively. They are available in JSON or CSV.

The delayed components list is more stable. See the [Data Source Caveats section](#data-source-caveats) for more information.

### JSON
`GET https://github.com/lawcal/sp500-components-history/raw/main/data/sp500_components.json`

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
`GET https://github.com/lawcal/sp500-components-history/raw/main/data/sp500_components.csv`

```
symbol,cik,name,sector
A,0001090872,Agilent Technologies,health_care
...
```

## Get Historical Components

Historical components can be retrieved by processing `components_history.csv` using the `date_added`, `date_removed` and `created_at` columns for filtering. Alternatively, use the `list_components()` helper function.

First, clone this repository and install the only external dependency:
```
pip install html-table-takeout
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

Data quality is only as reliable as the source. Because we rely on Wikipedia, it is common for unintentional or intentional inaccuracies to occur.

Below are the challenges faced while parsing page revisions with mitigations.

1. Components tracking with symbols [started on May 5, 2007](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=112958830).
    - Do not query for historical components before this date.
2. Company and symbol values mistakenly swapped or invalid. Example: [swapped headers](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=185113306), [invalid symbols](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=233849613)
    - Revision is skipped if there are any rows with symbols that cannot be parsed.
3. Duplicate entry is found in table. Example: [duplicate AIZ symbol](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=230711443)
    - Revision is skipped if duplicate symbols are found.
4. Table is malformed or missing. Example: [missing table tag](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=389847709), [vandalism](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=427992852)
    - Revision is skipped if table does not contain certain required headings.
5. Table formatting is broken. Example: [wide row due to unclosed tag](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=421482407)
    - Revision is skipped if the table is ragged.
6. Multiple symbol aliases due to various conventions. Example. [BRK-B](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=985987275)
    - Include a mapping of various aliases for the symbol and settle on one convention. Using the above example, it resolves to BRK.B.
7. Stock is added early before the actual effective date. Example: [TSLA added early](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=989167414)
    - If the duration of the addition is too short, it will be removed from the components history at a later revision.
8. Incorrect symbol is assigned to company. Example: [QQQ for 3M](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=555200632)
    - If the duration of the addition is too short, it will be removed from the components history at a later revision.
    - Furthermore, if a symbol is reinstated after a short time compared to the latest removal date, the old entry is restored with the removal date deleted.
9. Company changes ticker symbol. Example: [FB to META](https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&oldid=1092243288)
    - The revision date is saved when a symbol is first added under "created_at", allowing historical components lookups to filter out symbol changes that have not occurred yet.
    - This is a best effort mitigation as components inclusion and exclusion dates can be edited in later revisions and backdated.
    - The system does not track symbol changes.

## Developing

Install development dependencies:
```
pip install html-table-takeout mypy
```

To update your local data to the latest revision, run:
```
python bin/update.py
```

## Testing Instructions

Test code changes by deleting the `/data` and `/data_delayed` folders and running `python bin/update.py` to recreate the components history from scratch. Compare the generated files against their old ones.
