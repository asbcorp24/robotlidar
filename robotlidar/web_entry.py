#!/usr/bin/env python3
"""Reliable entry point for the RobotLidar web panel.

The original application keeps all ROS and API logic in ``web_app``. This
wrapper replaces the mounted StaticFiles route with explicit file responses,
which works consistently both from a source workspace and from an installed
ROS 2 package.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import uvicorn
from ament_index_python.packages import get_package_share_directory
from fastapi import HTTPException
from fastapi.responses import FileResponse

from robotlidar import web_app


def _find_static_directory() -> Path:
    candidates: list[Path] = []

    configured = os.environ.get('ROBOTLIDAR_STATIC_DIR')
    if configured:
        candidates.append(Path(configured).expanduser())

    try:
        candidates.append(
            Path(get_package_share_directory('robotlidar')) / 'web' / 'static'
        )
    except Exception:
        pass

    # systemd starts the application with the ROS workspace as WorkingDirectory.
    candidates.append(Path.cwd() / 'src' / 'robotlidar' / 'web' / 'static')

    module_path = Path(__file__).resolve()
    candidates.extend([
        module_path.parents[1] / 'web' / 'static',
        module_path.parents[2] / 'src' / 'robotlidar' / 'web' / 'static',
    ])

    required = ('index.html', 'style.css', 'app.js')
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_dir() and all((candidate / name).is_file() for name in required):
            return candidate

    checked = '\n'.join(f'  - {path}' for path in candidates)
    raise RuntimeError(
        'RobotLidar static directory was not found. Checked:\n' + checked
    )


STATIC_DIR = _find_static_directory()

# Make the existing root handler use the verified directory.
web_app.static_dir = STATIC_DIR

# Remove the old Starlette Mount('/static', ...). In some symlink-install
# layouts it points at a stale package directory and returns 404 for every file.
web_app.app.routes[:] = [
    route
    for route in web_app.app.routes
    if getattr(route, 'path', None) != '/static'
]


@web_app.app.get('/static/{filename}', include_in_schema=False)
def static_file(filename: str) -> FileResponse:
    allowed = {'style.css', 'app.js'}
    if filename not in allowed:
        raise HTTPException(status_code=404, detail='Static file not found')
    path = STATIC_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f'Static file missing: {filename}')
    return FileResponse(path)


@web_app.app.get('/api/debug/static', include_in_schema=False)
def debug_static() -> dict:
    return {
        'static_dir': str(STATIC_DIR),
        'files': {
            name: {
                'exists': (STATIC_DIR / name).is_file(),
                'size': (STATIC_DIR / name).stat().st_size
                if (STATIC_DIR / name).is_file()
                else None,
            }
            for name in ('index.html', 'style.css', 'app.js')
        },
    }


def main(args: Optional[list[str]] = None) -> None:
    del args
    uvicorn.run(
        web_app.app,
        host=web_app.HOST,
        port=web_app.PORT,
        log_level='info',
    )


if __name__ == '__main__':
    main()
