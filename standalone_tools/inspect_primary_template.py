import clang.cindex

CODE = """
template <typename T>
struct Outer {
    template <typename U>
    struct Inner {}; // Primary Template for Inner
};

// We specialize the Outer class for 'int'
template <>
template <typename U>
struct Outer<int>::Inner {
    U data;
};

// Partial specialization of Inner itself, still inside Outer<T>
// specializing on U's shape (pointer)
template <typename T>
template <typename U>
struct Outer<T>::Inner<U*> {
    U* ptr_data;
};
"""

def kind_name(cursor):
    return cursor.kind.name

def get_primary_template(cursor):
    try:
        pt = clang.cindex.conf.lib.clang_getSpecializedCursorTemplate(cursor)
        if pt and not pt.is_null():
            return pt

        print("Warning: Try again!")
        #This python binding is not reliable, should avoid using it.
        pt = cursor.specialized_cursor
        if pt and pt.kind != clang.cindex.CursorKind.NO_DECL_FOUND:
            return pt
    except Exception:
        pass
    return None

def describe(cursor, label):
    print(f"\n--- {label} ---")
    print(f"  Spelling      : {cursor.spelling}")
    print(f"  Kind          : {kind_name(cursor)}")
    pt = get_primary_template(cursor)
    if pt:
        print(f"  Primary Tmpl  : {pt.spelling}  (kind: {kind_name(pt)})")
    else:
        print(f"  Primary Tmpl  : (none / is the primary)")

def find_cursors(tu):
    results = {}

    def visit(cursor, depth=0):
        indent = "  " * depth
        # Uncomment to see all nodes:
        # print(f"{indent}{cursor.kind.name}: {cursor.spelling}")

        if cursor.spelling == "Outer":
            if cursor.kind.name == "CLASS_TEMPLATE":
                results.setdefault("Outer_primary", cursor)

        if cursor.spelling == "Inner":
            key = f"Inner_{cursor.kind.name}_{len([k for k in results if k.startswith('Inner')])}"
            results[key] = cursor

        for child in cursor.get_children():
            visit(child, depth + 1)

    visit(tu.cursor)
    return results

def main():
    index = clang.cindex.Index.create()
    tu = index.parse(
        "test.cpp",
        args=["-std=c++14"],
        unsaved_files=[("test.cpp", CODE)],
    )

    # Print any parse diagnostics
    for diag in tu.diagnostics:
        print(f"[DIAG] {diag.severity}: {diag.spelling}")

    cursors = find_cursors(tu)

    for label, cursor in cursors.items():
        describe(cursor, label)

if __name__ == "__main__":
    main()
