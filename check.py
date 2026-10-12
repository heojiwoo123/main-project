import pandas as pd
import matplotlib.pyplot as plt
watchlist_df = pd.read_csv("watchlist.csv", dtype={"code": str})
print(watchlist_df["code"].tolist())
dict(zip(watchlist_df["code"], watchlist_df["name"]))
