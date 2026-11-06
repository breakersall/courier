# list available recipes
default:
    @{{ just_executable() }} --list

# install dependencies, including the dev group
sync:
    uv sync

# run the test suite
test:
    uv run pytest

# format check, lint, type check
lint:
    uv run ruff format --check
    uv run ruff check
    uv run ty check

# auto-fix lint issues and format with ruff
format:
    uv run ruff check --fix
    uv run ruff format

# what CI runs
check: lint test

# delete all git-ignored files, including .venv (run `uv sync` after)
clean:
    git clean -fdX

# build the container image locally
docker-build:
    docker build -t courier .
