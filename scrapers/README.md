# Scraper project I/O convention

The runnable scraper projects use the same four-directory layout:

- `input/`: a `.tsv`, `.csv`, or `.xlsx` input file. When a directory is supplied, the first supported file in name order is used.
- `output/`: generated `.csv` data files and resumable checkpoint files.
- `config/`: YAML configuration. Relative paths are resolved from the YAML file's directory.
- `log/`: append-only JSON Lines logs, including failure reasons.

For Excel input, omit `sheetname` to read the first worksheet, or set it in YAML / pass `--sheetname NAME`.

## Commands

```powershell
# 4AFitment
cd scrapers/4afitment
npm install
npm run scrape -- --config config/4afitment.yaml --input input --output output --log log/4afitment.log

# Amazon.de
python scrapers/amazon-de/amazon_de_fitment_scraper.py --config scrapers/amazon-de/config/amazon_de.yaml

# Auto.ru catalog rank
python scrapers/auto-ru/auto_ru_catalog_rank_scraper.py --config scrapers/auto-ru/config/auto_ru.yaml

# Auto.ru model sales and gallery images
python scrapers/auto-ru/auto_ru_model_sales_scraper.py --config scrapers/auto-ru/config/auto_ru.yaml
python scrapers/auto-ru/auto_ru_gallery_image_scraper.py --config scrapers/auto-ru/config/auto_ru.yaml
```

Command-line arguments override YAML values. The `max` setting limits the number of input rows for Amazon.de and the two Auto.ru list-input jobs.
