"""Entry point used by GitHub Actions for scheduled and on-demand checks."""
import app


def main():
    app.sync_state_from_github()
    hotels, errors = app.run_checks()
    airports, airport_errors = app.refresh_airport_options()
    app.save_state_to_github()
    print(f"Price check completed: {hotels} hotels, {errors} errors")
    print(f"Airport options refreshed: {airports}, {airport_errors} errors")
    if errors or airport_errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

