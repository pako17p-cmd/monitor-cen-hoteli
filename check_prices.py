"""Entry point used by GitHub Actions for scheduled and on-demand checks."""
import app
import room_monitor


def main():
    app.sync_state_from_github()
    hotels, errors = app.run_checks()
    airports, airport_errors = app.refresh_airport_options()
    room_checks, room_errors = room_monitor.run_checks(app.connect, app.fetch_itaka_html)
    app.save_state_to_github()
    print(f"Price check completed: {hotels} hotels, {errors} errors")
    print(f"Airport options refreshed: {airports}, {airport_errors} errors")
    print(f"Room-count checks completed: {room_checks}, {room_errors} errors")
    if errors or airport_errors or room_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

