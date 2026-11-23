"""Entry point used by GitHub Actions for scheduled and on-demand checks."""
import app
import os
import room_monitor


def main():
    app.sync_state_from_github()
    hotels, errors = app.run_checks()
    airports, airport_errors = app.refresh_airport_options()
    # Room-count monitoring is opt-in and stays off unless explicitly enabled.
    if os.environ.get("ENABLE_ROOM_MONITORING", "").strip() == "1":
        room_checks, room_errors = room_monitor.run_checks(app.connect, app.fetch_itaka_html)
    else:
        room_checks, room_errors = 0, 0
        print("Room-count monitoring disabled; skipping room checks.")
    app.save_state_to_github()
    print(f"Price check completed: {hotels} hotels, {errors} errors")
    print(f"Airport options refreshed: {airports}, {airport_errors} errors")
    print(f"Room-count checks completed: {room_checks}, {room_errors} errors")
    if errors or airport_errors or room_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

