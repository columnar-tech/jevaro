import argparse

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="Run the Jevaro Arrow proxy")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("jevaro_server.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
