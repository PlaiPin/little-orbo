from setuptools import find_packages, setup

setup(
    name="orbo_runtime",
    version="0.1.0",
    packages=find_packages(),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/orbo_runtime"]),
        ("share/orbo_runtime", ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=False,
    maintainer="Orbo maintainers",
    maintainer_email="maintainers@orbo.invalid",
    description="Orbo runtime, peripheral nodes and bounded bench control",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "runtime = orbo_runtime.ros_entrypoints:runtime",
            "audio_check = orbo_runtime.ros_entrypoints:audio_check",
            "installed_smoke = orbo_runtime.installed_smoke:main",
            "base_bench = orbo_runtime.bench.base:main",
            "wheel_bench = orbo_runtime.bench.wheels:main",
            "flipper_bench = orbo_runtime.bench.flippers:main",
            "teleop_keyboard = orbo_runtime.teleop_keyboard:main",
            "velocity_gate = orbo_runtime.velocity_gate:main",
            "camera_monitor = orbo_runtime.camera:main",
        ],
    },
)
