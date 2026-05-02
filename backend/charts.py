import io
import logging
from datetime import datetime

logger = logging.getLogger(__name__)


def render_clicks_chart(data: list[dict], title: str) -> bytes:
    """
    Render a PNG bar chart of clicks per day.
    data: list of {"date": "2026-05-01", "count": 42}
    Returns PNG bytes.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        if not data:
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.text(0.5, 0.5, "Aucune donnée", ha="center", va="center", transform=ax.transAxes)
            ax.set_title(title)
        else:
            dates = [datetime.strptime(d["date"], "%Y-%m-%d") for d in data]
            counts = [d["count"] for d in data]

            fig, ax = plt.subplots(figsize=(10, 5))
            bars = ax.bar(dates, counts, color="#4A90D9", width=0.6)
            ax.set_title(title, fontsize=14, fontweight="bold", pad=15)
            ax.set_xlabel("Date", fontsize=11)
            ax.set_ylabel("Clics", fontsize=11)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%d/%m"))
            ax.xaxis.set_major_locator(mdates.DayLocator())
            plt.xticks(rotation=45, ha="right")

            for bar, count in zip(bars, counts):
                if count > 0:
                    ax.text(
                        bar.get_x() + bar.get_width() / 2.0,
                        bar.get_height(),
                        str(count),
                        ha="center",
                        va="bottom",
                        fontsize=9,
                    )

            ax.grid(axis="y", alpha=0.3)
            plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=100, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        logger.exception("Error rendering chart: %s", e)
        raise
