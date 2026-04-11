import clang.cindex

CODE = """
template <typename T>
struct Outer {
    template <typename U>
    struct TemplateInner {}; // Primary Template for TemplateInner
    struct StructInner {}; //Nested struct, not a template
    union UnionInner {}; //Nested union, not a template
};

/** 
 * Output:
 
--- Outer_primary ---
  Spelling      : Outer
  Kind          : CLASS_TEMPLATE
  Primary Tmpl  : (none / is the primary)

--- TemplateInner_CLASS_TEMPLATE_0 ---
  Spelling      : TemplateInner
  Kind          : CLASS_TEMPLATE
  Primary Tmpl  : (none / is the primary)

--- StructInner_STRUCT_DECL_0 ---
  Spelling      : StructInner
  Kind          : STRUCT_DECL
  Primary Tmpl  : (none / is the primary)

*/


// We specialize the Outer class for 'int'
template <>
template <typename U>
struct Outer<int>::TemplateInner {
    U data;
};

/**
 * Output:

--- TemplateInner_CLASS_TEMPLATE_1 ---
  Spelling      : TemplateInner
  Kind          : CLASS_TEMPLATE
  Primary Tmpl  : TemplateInner  (kind: CLASS_TEMPLATE)

*/


// Partial specialization of TemplateInner itself, still inside Outer<T>
// specializing on U's shape (pointer)
template <typename T>
template <typename U>
struct Outer<T>::TemplateInner<U*> {
    U* ptr_data;
};

/**
 * Output:

--- TemplateInner_CLASS_TEMPLATE_PARTIAL_SPECIALIZATION_2 ---
  Spelling      : TemplateInner
  Kind          : CLASS_TEMPLATE_PARTIAL_SPECIALIZATION
  Primary Tmpl  : TemplateInner  (kind: CLASS_TEMPLATE)
 
*/

// Full specialization of Outer and TemplateInner
template <>
template <>
struct Outer<int>::TemplateInner<int> {
    int* ptr_data;
};

/**
 * Output:

--- TemplateInner_STRUCT_DECL_3 ---
  Spelling      : TemplateInner
  Kind          : STRUCT_DECL
  Primary Tmpl  : TemplateInner  (kind: CLASS_TEMPLATE)

*/

// We specialize the Outer class for 'int'
template <>
struct Outer<int>::StructInner {
    int data;
};

/**
 * Output:

--- StructInner_STRUCT_DECL_1 ---
  Spelling      : StructInner
  Kind          : STRUCT_DECL
  Primary Tmpl  : StructInner  (kind: STRUCT_DECL)

*/

// We specialize the Outer class for 'int'
template <>
struct Outer<int>::UnionInner {
    int data;
};

/** 
 * Output:

--- UnionInner_UNION_DECL_1 ---
  Spelling      : UnionInner
  Kind          : UNION_DECL
  Primary Tmpl  : UnionInner  (kind: UNION_DECL)

*/

/**
 * Template metaprogramming with recursion
 */

template<typename T> struct ChainB; 

// General Case: A inherits from B with an extra pointer level
template<typename T>
struct ChainA : ChainB<T*> {}; 

// General Case: B inherits from A
template<typename T>
struct ChainB : ChainA<T> {};

// THE TERMINATOR: Specialization for a triple-pointer
// This version does NOT inherit from anything, breaking the cycle.
template<typename T>
struct ChainA<T***> {
    using type = T;
};


"""

def kind_name(cursor):
    return cursor.kind.name

def get_primary_template(cursor):
    try:
        pt = clang.cindex.conf.lib.clang_getSpecializedCursorTemplate(cursor)
        if pt and not pt.is_null():
            return pt

        #This python binding is not reliable, should avoid using it.
        #pt = cursor.specialized_cursor
        #if pt and pt.kind != clang.cindex.CursorKind.NO_DECL_FOUND:
        #    return pt
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

        if cursor.spelling == "TemplateInner":
            key = f"TemplateInner_{cursor.kind.name}_{len([k for k in results if k.startswith('TemplateInner')])}"
            results[key] = cursor

        if cursor.spelling == "StructInner":
            key = f"StructInner_{cursor.kind.name}_{len([k for k in results if k.startswith('StructInner')])}"
            results[key] = cursor

        if cursor.spelling == "UnionInner":
            key = f"UnionInner_{cursor.kind.name}_{len([k for k in results if k.startswith('UnionInner')])}"
            results[key] = cursor

        if cursor.spelling == "ChainA":
            key = f"ChainA_{cursor.kind.name}_{len([k for k in results if k.startswith('ChainA')])}"
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
