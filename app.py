import re
import shlex
from collections import OrderedDict

import streamlit as st


ENV_CONFIG = {
    "dev": {
        "s3_env": "dev",
        "connection_env": "DEV",
        "use_snapshot": True,
    },
    "qa": {
        "s3_env": "qa",
        "connection_env": "QA",
        "use_snapshot": False,
    },
    "prod": {
        "s3_env": "prd",
        "connection_env": "PROD",
        "use_snapshot": False,
    },
}

OUTPUT_ORDER = [
    "ScriptLocation",
    "JarLocation",
    "CommonPackageLocation",
    "TempS3Location",
    "GlueConnectionName",
]


def clean_input(text: str) -> str:
    """Remove an optional environment prefix such as 'dev:'."""

    return re.sub(
        r"^\s*(dev|qa|prod)\s*:\s*",
        "",
        text.strip(),
        flags=re.IGNORECASE,
    )


def parse_configuration(text: str) -> OrderedDict:
    """
    Parse whitespace-separated key=value configuration.

    shlex allows quoted values when a value contains spaces.
    """

    text = clean_input(text)

    if not text:
        raise ValueError("请输入配置内容。")

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ValueError(f"配置格式不正确：{exc}") from exc

    configuration = OrderedDict()

    for token in tokens:
        if "=" not in token:
            raise ValueError(
                f"无法识别配置项：{token}。配置项必须使用 Key=Value 格式。"
            )

        key, value = token.split("=", 1)
        key = key.strip()
        value = value.strip()

        if not key or not value:
            raise ValueError(f"配置项不完整：{token}")

        configuration[key] = value

    return configuration


def detect_source_environment(configuration: dict) -> str:
    """Detect dev, qa, or prod from S3 paths and Glue connection name."""

    detected_environments = set()

    for key, value in configuration.items():
        value_lower = value.lower()

        artifact_match = re.search(
            r"aws-glue-artifactory-(dev|qa|prd)-cn-north-1",
            value_lower,
        )
        if artifact_match:
            s3_env = artifact_match.group(1)
            detected_environments.add("prod" if s3_env == "prd" else s3_env)

        temp_match = re.search(
            r"aws-glue-etl-ccdh-(dev|qa|prd)-cn-north-1",
            value_lower,
        )
        if temp_match:
            s3_env = temp_match.group(1)
            detected_environments.add("prod" if s3_env == "prd" else s3_env)

        if key == "GlueConnectionName":
            connection_match = re.match(
                r"^(DEV|QA|PROD)-COMMON-GLUE-CONNECTION$",
                value,
                flags=re.IGNORECASE,
            )
            if connection_match:
                connection_env = connection_match.group(1).lower()
                detected_environments.add(connection_env)

    if not detected_environments:
        raise ValueError(
            "无法自动识别源环境，请通过页面上的“源环境”手动选择。"
        )

    if len(detected_environments) > 1:
        environments = ", ".join(sorted(detected_environments))
        raise ValueError(
            f"输入配置包含多个环境：{environments}，请检查配置内容。"
        )

    return detected_environments.pop()


def replace_s3_environment(value: str, target_environment: str) -> str:
    """Replace S3 environment names in Glue artifact and ETL buckets."""

    target_s3_env = ENV_CONFIG[target_environment]["s3_env"]

    value = re.sub(
        r"(aws-glue-artifactory-)(dev|qa|prd)(-cn-north-1)",
        rf"\g<1>{target_s3_env}\g<3>",
        value,
        flags=re.IGNORECASE,
    )

    value = re.sub(
        r"(aws-glue-etl-ccdh-)(dev|qa|prd)(-cn-north-1)",
        rf"\g<1>{target_s3_env}\g<3>",
        value,
        flags=re.IGNORECASE,
    )

    return value


def convert_package_version(
    value: str,
    target_environment: str,
) -> str:
    """
    Convert package version and snapshot suffix.

    Rules:
    - dev:  v1.16.0.0-snapshot.jar
    - qa:   v1.16.0.0.jar
    - prod: v1.16.0.jar
    """

    pattern = re.compile(
        r"v(?P<version>\d+(?:\.\d+){2,3})"
        r"(?:-snapshot)?"
        r"(?P<extension>\.(?:jar|zip))",
        flags=re.IGNORECASE,
    )

    def replace_version(match: re.Match) -> str:
        version_parts = match.group("version").split(".")
        extension = match.group("extension")

        if target_environment in {"dev", "qa"}:
            # PROD 的三段版本转换到 DEV 或 QA 时，补充最后一个 .0。
            #
            # v1.16.0 -> v1.16.0.0
            # v1.19.0 -> v1.19.0.0
            if len(version_parts) == 3:
                version_parts.append("0")

        elif target_environment == "prod":
            # DEV 或 QA 的四段版本转换到 PROD 时，
            # 删除最后一个构建版本 .0。
            #
            # v1.16.0.0 -> v1.16.0
            # v1.19.0.0 -> v1.19.0
            if len(version_parts) == 4 and version_parts[-1] == "0":
                version_parts.pop()

        converted_version = ".".join(version_parts)

        if target_environment == "dev":
            suffix = "-snapshot"
        else:
            suffix = ""

        return f"v{converted_version}{suffix}{extension}"

    return pattern.sub(replace_version, value)


def transform_value(
    key: str,
    value: str,
    target_environment: str,
) -> str:
    """Transform one configuration value for the target environment."""

    if key in {
        "ScriptLocation",
        "JarLocation",
        "CommonPackageLocation",
        "TempS3Location",
    }:
        value = replace_s3_environment(value, target_environment)

    if key in {"JarLocation", "CommonPackageLocation"}:
        value = convert_package_version(
            value=value,
            target_environment=target_environment,
        )

    if key == "GlueConnectionName":
        connection_env = ENV_CONFIG[target_environment]["connection_env"]
        value = f"{connection_env}-COMMON-GLUE-CONNECTION"

    return value


def convert_configuration(
    configuration: dict,
    target_environment: str,
) -> OrderedDict:
    """Convert all configuration values to the target environment."""

    converted = OrderedDict()

    for key, value in configuration.items():
        converted[key] = transform_value(
            key=key,
            value=value,
            target_environment=target_environment,
        )

    return converted


def format_configuration(configuration: dict) -> str:
    """Format the configuration as one line of Key=Value pairs."""

    ordered_keys = [
        key for key in OUTPUT_ORDER if key in configuration
    ]

    ordered_keys.extend(
        key for key in configuration if key not in ordered_keys
    )

    return " ".join(
        f"{key}={configuration[key]}"
        for key in ordered_keys
    )


def main() -> None:
    st.set_page_config(
        page_title="Glue Environment Configuration Converter",
        layout="wide",
    )

    st.title("Glue 环境配置转换工具")

    st.write(
        "输入 dev、qa 或 prod 任意一个环境的配置，"
        "自动生成其他环境的配置。"
    )

    source_environment_option = st.selectbox(
        "源环境",
        options=["自动识别", "dev", "qa", "prod"],
        index=0,
    )

    example = ""

    input_text = st.text_area(
        "输入配置",
        value=example,
        height=220,
        placeholder="请输入 Key=Value 格式的配置",
    )

    if st.button("生成环境配置", type="primary"):
        try:
            source_configuration = parse_configuration(input_text)

            if source_environment_option == "自动识别":
                source_environment = detect_source_environment(
                    source_configuration
                )
            else:
                source_environment = source_environment_option

            st.success(f"识别到的源环境：{source_environment}")

            target_environments = [
                environment
                for environment in ENV_CONFIG
                if environment != source_environment
            ]

            tabs = st.tabs(
                [
                    environment.upper()
                    for environment in target_environments
                ]
            )

            for tab, target_environment in zip(
                tabs,
                target_environments,
            ):
                with tab:
                    converted = convert_configuration(
                        configuration=source_configuration,
                        target_environment=target_environment,
                    )

                    result = format_configuration(converted)

                    st.subheader(
                        f"{target_environment.upper()} 配置"
                    )
                    st.code(result, language="text")

            st.divider()
            st.subheader("全部环境配置")

            for environment in ENV_CONFIG:
                converted = convert_configuration(
                    configuration=source_configuration,
                    target_environment=environment,
                )

                st.markdown(f"**{environment.upper()}**")
                st.code(
                    format_configuration(converted),
                    language="text",
                )

        except ValueError as exc:
            st.error(str(exc))


if __name__ == "__main__":
    main()
