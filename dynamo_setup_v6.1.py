import boto3

dynamodb = boto3.resource('dynamodb', region_name='ap-south-1')

tables = [
    {
        "TableName": "TradingBot_Trades",
        "KeySchema": [
            {"AttributeName": "trade_id", "KeyType": "HASH"},
            {"AttributeName": "trade_date", "KeyType": "RANGE"}
        ],
        "AttributeDefinitions": [
            {"AttributeName": "trade_id", "AttributeType": "S"},
            {"AttributeName": "trade_date", "AttributeType": "S"}
        ]
    },
    {
        "TableName": "TradingBot_DailyState",
        "KeySchema": [
            {"AttributeName": "date", "KeyType": "HASH"}
        ],
        "AttributeDefinitions": [
            {"AttributeName": "date", "AttributeType": "S"}
        ]
    },
    {
        "TableName": "TradingBot_ActiveTrade",
        "KeySchema": [
            {"AttributeName": "id", "KeyType": "HASH"}
        ],
        "AttributeDefinitions": [
            {"AttributeName": "id", "AttributeType": "S"}
        ]
    },
    {
        "TableName": "TradingBot_OrderAudit",
        "KeySchema": [
            {"AttributeName": "order_id", "KeyType": "HASH"},
            {"AttributeName": "timestamp", "KeyType": "RANGE"}
        ],
        "AttributeDefinitions": [
            {"AttributeName": "order_id", "AttributeType": "S"},
            {"AttributeName": "timestamp", "AttributeType": "S"}
        ]
    }
]

for table_config in tables:
    try:
        table = dynamodb.create_table(
            TableName=table_config["TableName"],
            KeySchema=table_config["KeySchema"],
            AttributeDefinitions=table_config["AttributeDefinitions"],
            BillingMode="PAY_PER_REQUEST"
        )
        table.wait_until_exists()
        print(f"Created: {table_config['TableName']}")
    except Exception as e:
        if "ResourceInUseException" in str(e):
            print(f"Already exists: {table_config['TableName']}")
        else:
            print(f"Error: {table_config['TableName']} - {e}")

print("All tables ready")
