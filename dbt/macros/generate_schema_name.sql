{# Use the custom schema name verbatim (analytics_staging -> staging) for readable schemas. #}
{% macro generate_schema_name(custom_schema_name, node) -%}
    {%- if custom_schema_name is none -%}{{ target.schema }}{%- else -%}dbt_{{ custom_schema_name | trim }}{%- endif -%}
{%- endmacro %}
