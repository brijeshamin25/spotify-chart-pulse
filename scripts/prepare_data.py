"""
prepare_data.py

Turns the raw daily Spotify Top 50 (Global) export into a small star schema
that loads cleanly into Power BI:

    fact_chart_daily  - one row per song per chart day
    dim_song          - one row per song, with longevity / peak stats
    dim_artist        - one row per individual artist
    bridge_song_artist- links songs to every credited artist
    dim_date          - calendar table covering the chart period

Run from the project root:
    python scripts/prepare_data.py
"""

from pathlib import Path
import hashlib

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW_FILE = ROOT / "data" / "raw" / "spotify-top-50-world.xlsx"
OUT_DIR = ROOT / "data" / "processed"

# Artist names that contain "&" but are one act, not a collab.
# Found these by eyeballing the unique artist list - add more if the data changes.
SINGLE_ACTS_WITH_AMPERSAND = {
    "Fast & Furious: The Fast Saga",
    'Bobby "Boris" Pickett & The Crypt-Kickers',
    "Daryl Hall & John Oates",
    "Richy Mitch & The Coal Miners",
}


def make_id(*parts) -> str:
    """Short stable id so keys don't change between runs."""
    text = "|".join(str(p) for p in parts)
    return hashlib.md5(text.encode("utf-8")).hexdigest()[:10]


def parse_release_date(value):
    # The column is mostly real dates, but a few rows only have a year (int)
    # or a "YYYY-MM" string. Pad those out to the first of the month/year.
    if isinstance(value, int):
        return pd.Timestamp(year=value, month=1, day=1)
    if isinstance(value, str) and len(value) == 7:
        return pd.Timestamp(value + "-01")
    return pd.to_datetime(value, errors="coerce")


def split_artists(name: str) -> list[str]:
    if name in SINGLE_ACTS_WITH_AMPERSAND:
        return [name]
    return [a.strip() for a in name.split(" & ") if a.strip()]


def longevity_tier(days: int) -> str:
    if days <= 7:
        return "1. Flash (≤1 week)"
    if days <= 60:
        return "2. Short run (≤2 months)"
    if days <= 180:
        return "3. Long run (≤6 months)"
    return "4. Evergreen (6+ months)"


def main():
    raw = pd.read_excel(RAW_FILE)
    print(f"Loaded {len(raw):,} rows from {RAW_FILE.name}")

    df = raw.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    df["release_date"] = df["release_date"].apply(parse_release_date)
    df["song_id"] = [make_id(s, a) for s, a in zip(df["song"], df["artist"])]

    # ---- fact table -------------------------------------------------------
    df = df.sort_values(["song_id", "date"])
    df["prev_position"] = df.groupby("song_id")["position"].shift(1)
    df["prev_date"] = df.groupby("song_id")["date"].shift(1)

    # Only count it as a move if the song was on the chart the day before.
    # A gap means it dropped out and came back (a re-entry).
    gap_days = (df["date"] - df["prev_date"]).dt.days
    consecutive = gap_days.isin([1, 2])  # 2 covers the few missing days in the source
    df.loc[~consecutive, "prev_position"] = pd.NA
    df["position_change"] = df["prev_position"] - df["position"]  # positive = climbed
    df["is_reentry"] = gap_days.gt(2).astype(int)
    df["is_debut"] = df["prev_date"].isna().astype(int)
    df["is_top10"] = (df["position"] <= 10).astype(int)
    df["is_number1"] = (df["position"] == 1).astype(int)

    fact = df[[
        "date", "song_id", "position", "prev_position", "position_change",
        "popularity", "is_top10", "is_number1", "is_debut", "is_reentry",
    ]].sort_values(["date", "position"])

    # ---- song dimension ---------------------------------------------------
    first_date = df["date"].min()
    song = (
        df.groupby("song_id")
        .agg(
            song=("song", "first"),
            artist_credit=("artist", "first"),
            first_chart_date=("date", "min"),
            last_chart_date=("date", "max"),
            days_on_chart=("date", "count"),
            peak_position=("position", "min"),
            avg_position=("position", "mean"),
            days_in_top10=("is_top10", "sum"),
            days_at_number1=("is_number1", "sum"),
            reentries=("is_reentry", "sum"),
            avg_popularity=("popularity", "mean"),
            release_date=("release_date", "min"),  # earliest = original single release
            duration_ms=("duration_ms", "median"),
            is_explicit=("is_explicit", "max"),
            album_type=("album_type", lambda s: s.mode().iat[0]),
            album_cover_url=("album_cover_url", "first"),
        )
        .reset_index()
    )

    song["avg_position"] = song["avg_position"].round(1)
    song["avg_popularity"] = song["avg_popularity"].round(1)
    song["duration_min"] = (song["duration_ms"] / 60000).round(2)
    song["release_year"] = song["release_date"].dt.year
    song["days_release_to_chart"] = (song["first_chart_date"] - song["release_date"]).dt.days
    song["longevity_tier"] = song["days_on_chart"].apply(longevity_tier)
    # Songs already charting on day one of the data started before the window,
    # so their true run is longer than what we see. Flag them.
    song["already_charting_at_start"] = (song["first_chart_date"] == first_date).astype(int)
    song["catalog_song"] = (song["release_year"] < 2020).astype(int)
    song["is_explicit"] = song["is_explicit"].astype(int)
    song = song.drop(columns=["duration_ms"])

    # ---- artists + bridge -------------------------------------------------
    bridge_rows = []
    for row in song[["song_id", "artist_credit"]].itertuples(index=False):
        for i, name in enumerate(split_artists(row.artist_credit)):
            bridge_rows.append({
                "song_id": row.song_id,
                "artist_id": make_id(name),
                "artist": name,
                "is_lead_artist": int(i == 0),
            })
    bridge = pd.DataFrame(bridge_rows)
    song["lead_artist"] = song["song_id"].map(
        bridge[bridge["is_lead_artist"] == 1].set_index("song_id")["artist"]
    )

    artist = (
        bridge.merge(song[["song_id", "days_on_chart", "peak_position"]], on="song_id")
        .groupby(["artist_id", "artist"])
        .agg(
            songs_charted=("song_id", "nunique"),
            total_chart_days=("days_on_chart", "sum"),
            best_peak=("peak_position", "min"),
        )
        .reset_index()
        .sort_values("total_chart_days", ascending=False)
    )
    bridge = bridge.drop(columns=["artist"])

    # ---- date dimension ---------------------------------------------------
    dates = pd.DataFrame({"date": pd.date_range(df["date"].min(), df["date"].max())})
    dates["year"] = dates["date"].dt.year
    dates["quarter"] = "Q" + dates["date"].dt.quarter.astype(str)
    dates["month_num"] = dates["date"].dt.month
    dates["month"] = dates["date"].dt.strftime("%b")
    dates["year_month"] = dates["date"].dt.strftime("%Y-%m")
    dates["weekday"] = dates["date"].dt.strftime("%a")
    dates["weekday_num"] = dates["date"].dt.weekday + 1
    dates["has_chart_data"] = dates["date"].isin(df["date"].unique()).astype(int)

    # ---- save -------------------------------------------------------------
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    outputs = {
        "fact_chart_daily.csv": fact,
        "dim_song.csv": song,
        "dim_artist.csv": artist,
        "bridge_song_artist.csv": bridge,
        "dim_date.csv": dates,
    }
    for name, table in outputs.items():
        table.to_csv(OUT_DIR / name, index=False, date_format="%Y-%m-%d")
        print(f"  {name:<24} {len(table):>6,} rows")

    missing = dates.loc[dates["has_chart_data"] == 0, "date"].dt.date.tolist()
    print(f"\nChart days missing from source: {missing}")
    print("Done.")


if __name__ == "__main__":
    main()
