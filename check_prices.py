"""Entry point used by GitHub Actions for scheduled and on-demand checks."""
import app


def main():
    app.sync_state_from_github()
    hotels, errors = app.run_checks()
    app.save_state_to_github()
    print(f"Price check completed: {hotels} hotels, {errors} errors")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

