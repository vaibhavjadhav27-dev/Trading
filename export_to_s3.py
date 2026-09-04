
#!/usr/bin/env python3
import boto3
import csv
import os
from datetime import datetime
from decimal import Decimal

REGION = "ap-south-1"
S3_BUCKET = "vaibhav-trading-bot-exports"
S3_PREFIX = "dynamodb-exports"
TEMP_DIR = "/tmp/dynamo_exports"

TABLES_TO_EXPORT = [
    "Bot_State",
    "TradingBot_ActiveTrade",
    "TradingBot_CandidateLog",
    "TradingBot_DailyLearnings",
    "TradingBot_DailyState",
    "TradingBot_OrderAudit",
    "TradingBot_PriceLog",
    "TradingBot_TradeHistory",
    "TradingBot_Trades",
]

dynamodb = boto3.resource("dynamodb", region_name=REGION)
s3 = boto3.client("s3", region_name=REGION)


def decimal_to_float(obj):
    if isinstance(obj, Decimal):
        return float(obj)
    elif isinstance(obj, dict):
        return {k: decimal_to_float(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [decimal_to_float(i) for i in obj]
    return obj


def scan_full_table(table_name):
    table = dynamodb.Table(table_name)
    items = []
    print("  Scanning " + table_name + "...")
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    print("  Found " + str(len(items)) + " records")
    return items


def export_table_to_csv(table_name, items):
    if not items:
        print("  Skipping " + table_name + " - no records")
        return None
    items = [decimal_to_float(item) for item in items]
    all_keys = set()
    for item in items:
        all_keys.update(item.keys())
    all_keys = sorted(all_keys)
    os.makedirs(TEMP_DIR, exist_ok=True)
    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = TEMP_DIR + "/" + table_name + "_" + date_str + ".csv"
    with open(filename, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow(item)
    print("  Saved to " + filename)
    return filename


def upload_to_s3(filepath, table_name):
    if filepath is None:
        return
    filename = os.path.basename(filepath)
    s3_key = S3_PREFIX + "/" + table_name + "/" + filename
    s3.upload_file(filepath, S3_BUCKET, s3_key)
    print("  Uploaded to s3://" + S3_BUCKET + "/" + s3_key)
    os.remove(filepath)


def create_bucket_if_not_exists():
    try:
        s3.head_bucket(Bucket=S3_BUCKET)
        print("Bucket exists: " + S3_BUCKET)
    except Exception:
        print("Creating bucket: " + S3_BUCKET)
        s3.create_bucket(
            Bucket=S3_BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        print("Bucket created: " + S3_BUCKET)


def main():
    print("=" * 50)
    print("DynamoDB to S3 Export | " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("=" * 50)
    create_bucket_if_not_exists()
    total_records = 0
    for table_name in TABLES_TO_EXPORT:
        print("")
        print("Exporting: " + table_name)
        try:
            items = scan_full_table(table_name)
            filepath = export_table_to_csv(table_name, items)
            upload_to_s3(filepath, table_name)
            total_records += len(items)
        except Exception as e:
            print("  Error exporting " + table_name + ": " + str(e))
    print("")
    print("=" * 50)
    print("Done! Exported " + str(total_records) + " total records from " + str(len(TABLES_TO_EXPORT)) + " tables")
    print("Location: s3://" + S3_BUCKET + "/" + S3_PREFIX + "/")
    print("=" * 50)


if __name__ == "__main__":
    main()

