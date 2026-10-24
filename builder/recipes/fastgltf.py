from __future__ import annotations

STAMP_REVISION = "1"


def enabled(builder, _repo) -> bool:
    return bool(builder.config.global_cfg.build_fastgltf)


def cmake_args(builder, ctx) -> list[str]:
    # Select the packages installed by the dependency recipes in this prefix.
    cmake_dir = ctx.install_prefix / "lib" / "cmake"
    return [
        f"-Dsimdjson_DIR={builder._cmake_path_arg(cmake_dir / 'simdjson')}",
        f"-Dsimdutf_DIR={builder._cmake_path_arg(cmake_dir / 'simdutf')}",
    ]
