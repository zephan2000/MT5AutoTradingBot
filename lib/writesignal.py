# examples/basic.py
from lib.supa import service_client

sb = service_client()

# Insert a signal
inserted = sb.table("signals").insert({
    "source": "telegram",
    "strategy_id": "00000000-0000-0000-0000-000000000000",  # optional
    "master_id": "YOUR-AUTH-UUID-HERE",
    "symbol": "EURUSD",
    "side": "buy",
    "size": 0.1,
    "sl": 1.0932,
    "tp": [1.0950, 1.0975],
    
}).execute()
print(inserted.data)

# RPC create order (preferred)
order = sb.rpc("rpc_create_order", {
    "p_account_id": "ACCOUNT-UUID",
    "p_signal_id": inserted.data[0]["id"],
    "p_client_order_id": "tg-12345",
    "p_meta": {"source_msg_id": 12345}
}).execute()
print(order.data)
