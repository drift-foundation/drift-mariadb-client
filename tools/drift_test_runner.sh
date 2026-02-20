#!/usr/bin/env bash
set -euo pipefail

usage() {
	echo "usage: $0 <run-all|run-one|compile|compile-one> [--src-root DIR] [--test-root DIR] [--test-file FILE] [--file FILE] [--target-word-bits N]" >&2
	exit 2
}

MODE="${1:-}"
[[ -n "${MODE}" ]] || usage
shift || true

SRC_ROOTS=()
SRC_ROOTS+=("packages/mariadb-wire-proto/src")
SRC_ROOTS_OVERRIDDEN=0
TEST_ROOT="packages/mariadb-wire-proto/tests/unit"
TEST_FILE=""
CHECK_FILE=""
TARGET_WORD_BITS="64"

while [[ $# -gt 0 ]]; do
	case "$1" in
		--src-root)
			if [[ "${SRC_ROOTS_OVERRIDDEN}" == "0" ]]; then
				SRC_ROOTS=()
				SRC_ROOTS_OVERRIDDEN=1
			fi
			SRC_ROOTS+=("${2:-}")
			shift 2
			;;
		--test-root)
			TEST_ROOT="${2:-}"
			shift 2
			;;
		--test-file)
			TEST_FILE="${2:-}"
			shift 2
			;;
		--file)
			CHECK_FILE="${2:-}"
			shift 2
			;;
		--target-word-bits)
			TARGET_WORD_BITS="${2:-}"
			shift 2
			;;
		*)
			echo "error: unknown arg '$1'" >&2
			usage
			;;
	esac
done

require_driftc() {
	: "${DRIFTC:?set DRIFTC to your driftc path}"
}

guard_incompat() {
	if [[ "${DRIFT_ASAN:-0}" == "1" && ( "${DRIFT_MEMCHECK:-0}" == "1" || "${DRIFT_MASSIF:-0}" == "1" ) ]]; then
		echo "error: DRIFT_ASAN is incompatible with DRIFT_MEMCHECK/DRIFT_MASSIF in the same run" >&2
		exit 2
	fi
}

guard_compile_only_flags() {
	if [[ "${DRIFT_MEMCHECK:-0}" == "1" || "${DRIFT_MASSIF:-0}" == "1" ]]; then
		echo "error: compile-only mode does not accept DRIFT_MEMCHECK/DRIFT_MASSIF; use run-all/run-one" >&2
		exit 2
	fi
}

set_asan_defaults_for_run() {
	if [[ "${DRIFT_ASAN:-0}" == "1" && -z "${ASAN_OPTIONS:-}" ]]; then
		export ASAN_OPTIONS="detect_leaks=0:halt_on_error=1"
	fi
}

collect_src_files() {
	SRC_FILES=()
	local root
	for root in "${SRC_ROOTS[@]}"; do
		if [[ -d "${root}" ]]; then
			while IFS= read -r f; do
				SRC_FILES+=("${f}")
			done < <(find "${root}" -type f -name "*.drift" | sort)
		fi
	done
	if [[ "${#SRC_FILES[@]}" -eq 0 ]]; then
		echo "error: no source files found under src roots: ${SRC_ROOTS[*]}" >&2
		exit 2
	fi
}

is_executable_test_entry() {
	local file="$1"
	rg -n "^module[[:space:]]+" "${file}" >/dev/null 2>&1 && rg -n "^fn[[:space:]]+main\\(" "${file}" >/dev/null 2>&1
}

test_module_of() {
	local file="$1"
	sed -n "s/^module[[:space:]]\\+\\(.*\\)$/\\1/p" "${file}" | head -n1
}

compile_only_file() {
	local file="$1"
	[[ -f "${file}" ]] || { echo "error: missing file ${file}" >&2; exit 2; }
	env -u DRIFT_MEMCHECK -u DRIFT_MASSIF "${DRIFTC}" --target-word-bits "${TARGET_WORD_BITS}" "${SRC_FILES[@]}" "${file}"
}

compile_test_binary() {
	local test_file="$1"
	local bin_path="$2"
	local test_module
	test_module="$(test_module_of "${test_file}")"
	[[ -n "${test_module}" ]] || { echo "error: missing module declaration in ${test_file}" >&2; exit 2; }
	local entry_symbol="${test_module}::main"
	env -u DRIFT_MEMCHECK -u DRIFT_MASSIF "${DRIFTC}" --target-word-bits "${TARGET_WORD_BITS}" --entry "${entry_symbol}" "${SRC_FILES[@]}" "${test_file}" -o "${bin_path}"
}

run_binary() {
	local bin_path="$1"
	if [[ "${DRIFT_MEMCHECK:-0}" == "1" ]]; then
		valgrind --tool=memcheck --error-exitcode=97 --leak-check=full "${bin_path}"
	elif [[ "${DRIFT_MASSIF:-0}" == "1" ]]; then
		valgrind --tool=massif --error-exitcode=97 "${bin_path}"
	else
		"${bin_path}"
	fi
}

run_all_tests() {
	mapfile -t CANDIDATE_TEST_FILES < <(find "${TEST_ROOT}" -type f -name "*.drift" | sort)
	TEST_FILES=()
	local f
	for f in "${CANDIDATE_TEST_FILES[@]}"; do
		if is_executable_test_entry "${f}"; then
			TEST_FILES+=("${f}")
		fi
	done
	if [[ "${#TEST_FILES[@]}" -eq 0 ]]; then
		echo "error: no executable test entry files found under ${TEST_ROOT}" >&2
		exit 2
	fi
	local out_dir
	out_dir="$(mktemp -d /tmp/drift-test-runner.XXXXXX)"
	trap "rm -rf '${out_dir}'" EXIT
	local test_file
	for test_file in "${TEST_FILES[@]}"; do
		local test_name
		test_name="$(basename "${test_file}" .drift)"
		local bin_path="${out_dir}/${test_name}"
		echo "compile ${test_name}"
		compile_test_binary "${test_file}" "${bin_path}"
		echo "run ${test_name}"
		run_binary "${bin_path}"
	done
}

run_one_test() {
	[[ -f "${TEST_FILE}" ]] || { echo "error: missing test file ${TEST_FILE}" >&2; exit 2; }
	is_executable_test_entry "${TEST_FILE}" || { echo "error: ${TEST_FILE} is not an executable test entry (requires module + fn main)" >&2; exit 2; }
	local out_dir
	out_dir="$(mktemp -d /tmp/drift-test-runner-one.XXXXXX)"
	trap "rm -rf '${out_dir}'" EXIT
	local bin_path="${out_dir}/unit-test"
	echo "compile $(basename "${TEST_FILE}")"
	compile_test_binary "${TEST_FILE}" "${bin_path}"
	echo "run $(basename "${TEST_FILE}")"
	run_binary "${bin_path}"
}

require_driftc
guard_incompat
collect_src_files

case "${MODE}" in
	compile)
	guard_compile_only_flags
	[[ -n "${CHECK_FILE}" ]] || { echo "error: --file is required for compile mode" >&2; exit 2; }
	compile_only_file "${CHECK_FILE}"
	;;
	compile-one)
	guard_compile_only_flags
	[[ -n "${CHECK_FILE}" ]] || { echo "error: --file is required for compile-one mode" >&2; exit 2; }
	compile_only_file "${CHECK_FILE}"
	;;
	run-all)
	set_asan_defaults_for_run
	run_all_tests
	;;
	run-one)
	[[ -n "${TEST_FILE}" ]] || { echo "error: --test-file is required for run-one mode" >&2; exit 2; }
	set_asan_defaults_for_run
	run_one_test
	;;
	*)
	usage
	;;
esac
