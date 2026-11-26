# Optimize Dockerfile ./common/lesson_5/docker-lab/Dockerfile.

## Objectivities

Optimize the Dockerfile and determine optimization value after each step.

* Use multistage
* Chose base image for runtime (slim / alpine / distroless). Explain why a specific one is selected
* Do not use dev-dependencies in runtime. Used runtime dependencies listed in ./common/lesson_5/docker-lab/pyproject.toml
* The final image doesn't contain odd files (logs, caches, debug, dumps, etc)

## Steps


| Step | Size | delta |
|---|---|---|
| baseline|...| na|
| + .dockerignore|... |... |
| + slim base image|...|...|
| + multi-stage without build-deps|...|...|
| + without dev and unnecessary dependencies|...|...|


* Create Dockerfile for each step - Dockerfile-stepN
* Use the script `./common/lesson/docker-lab/scripts/bench.sh stepN-name -f Dockerfile.step1`
* Fill out the table after each step

## Caveats

* Do not touch origonal Dockerfile. Create new file for each optimization step.
* Do not touch `app/`, `test`
* The task is to put the packaging in order without breaking the behavior.

## Readiness criteria
* size <= 400 MB
* main process started not from root user
* there is HEALTHCHECK
* there is no compiler in runtime
* the is no dev-tools in the image.
* there is no tests and trash in the image
* There is no secrets in ENV and history
* .dockerignore exists
* hadolint no warning
* the optimized image must serve endpoints. Create smoke script with endpoint checks. The script must return error if any check failed.
    - GET  /health → 200
    - GET  /api/v1/launches  → 200, return five objects
    - POST /api/v1/launches/LC-101/countdown → 200, t_minus_seconds = 600
    - POST /api/v1/launches/LC-102/countdown      → 409 (no-go by weather)
* Create Result.md with sections:
  - Original State. from scripts `bench.sh baseline` + `score.sh baseline` before optimization. And output from hadolint/trivy/dive.
  - Choosing the base image
  - Compare by tools. Compare hadolint/trivy/dive before and after.

