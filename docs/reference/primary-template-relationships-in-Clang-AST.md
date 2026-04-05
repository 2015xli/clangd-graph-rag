# Primary Template Relationships in Clang's AST


## What kinds can BE a primary template?

There are three cursor kinds that can serve as a primary template:
________________________________________
### Case 1: CLASS_TEMPLATE — the normal case
```
template <typename T>
struct Foo {};  // CLASS_TEMPLATE — is the primary template
```
________________________________________
### Case 2: CLASS_DECL/STRUCT_DECL — a non-templated nested class inside a class template
```
template <typename T>
struct Outer {
    struct Inner {};  // CLASS_DECL/STRUCT_DECL — but acts as primary template for Inner's specializations
};
```
Inner has no template parameters of its own, so clang gives it a CLASS_DECL or STRUCT_DECL cursor, not a CLASS_TEMPLATE. Yet it can still be the primary template for specializations of Inner scoped to explicit specializations of Outer.
________________________________________
### Case 3: CLASS_TEMPLATE (member template redefined in explicit outer specialization)
```
template <typename T>
struct Outer {
    template <typename U>
    struct Inner {};  // CLASS_TEMPLATE — primary template for Inner
};

template <>
template <typename U>
struct Outer<int>::Inner {  // CLASS_TEMPLATE — has primary template: the Inner above
    U data;
};
```
The second Inner is itself a CLASS_TEMPLATE, but it points back to the first Inner (CLASS_TEMPLATE) as its primary. So a CLASS_TEMPLATE can appear on both sides of the relationship.
________________________________________


## What kinds CAN HAVE a primary template

There are three cursor kinds that can have a primary template (i.e., clang_getSpecializedCursorTemplate returns non-null):
________________________________________
### Case A: CLASS_TEMPLATE_PARTIAL_SPECIALIZATION — always has a primary template

Primary is always a CLASS_TEMPLATE:
```
template <typename T>
struct Foo {};           // CLASS_TEMPLATE — primary

template <typename T>
struct Foo<T*> {};       // CLASS_TEMPLATE_PARTIAL_SPECIALIZATION
                         //   → primary: Foo (CLASS_TEMPLATE)
Primary can also be a CLASS_TEMPLATE that itself has a primary (the member redefinition case):
template <typename T>
struct Outer {
    template <typename U>
    struct Inner {};     // CLASS_TEMPLATE — primary (no further primary)
};

template <>
template <typename U>
struct Outer<int>::Inner { };   // CLASS_TEMPLATE
                                //   → primary: Outer<T>::Inner (CLASS_TEMPLATE)

template <typename T>
template <typename U>
struct Outer<T>::Inner<U*> { }; // CLASS_TEMPLATE_PARTIAL_SPECIALIZATION
                                //   → primary: Outer<T>::Inner (CLASS_TEMPLATE)
```
________________________________________
### Case B: CLASS_DECL (explicit full specialization) — has a primary template

Primary is a CLASS_TEMPLATE:
```
template <typename T>
struct Foo {};      // CLASS_TEMPLATE — primary

template <>
struct Foo<int> {}; // CLASS_DECL (explicit full specialization)
                    //   → primary: Foo (CLASS_TEMPLATE)

Primary is a CLASS_DECL/STRUCT_DECL (the nested non-templated class case):
template <typename T>
struct Outer {
    struct Inner {};    // CLASS_DECL/STRUCT_DECL/UNION_DECL — primary template for Inner
};

template <>
struct Outer<int>::Inner {  // CLASS_DECL/STRUCT_DECL/UNION_DECL (explicit specialization)
                            //   → primary: Outer<T>::Inner (CLASS_DECL/STRUCT_DECL/UNION_DECL)
};
```
________________________________________
### Case C: CLASS_TEMPLATE (member template redefined in explicit outer specialization)

As shown above, primary is always a CLASS_TEMPLATE:
```
template <typename T>
struct Outer {
    template <typename U>
    struct Inner {};        // CLASS_TEMPLATE — primary (no further primary)
};

template <>
template <typename U>
struct Outer<int>::Inner {  // CLASS_TEMPLATE
                            //   → primary: Outer<T>::Inner (CLASS_TEMPLATE)
};
```
________________________________________
## Summary table

| Cursor Kind	                        | Can be a primary template	                                             | Can have a primary template                                                          |
|---------------------------------------|------------------------------------------------------------------------|--------------------------------------------------------------------------------------|
| CLASS_TEMPLATE	                    | V Always	                                                             | V Only when it's a member template redefined inside an explicit outer specialization |   
| CLASS_DECL/STRUCT_DECL/UNION_DECL	    | V Only when it's a non-templated nested class inside a CLASS_TEMPLATE	 | V When it or its outer class is an explicit full specialization                      |
| CLASS_TEMPLATE_PARTIAL_SPECIALIZATION	| X Never	                                                             | V Always                                                                             |

Termination condition when walking the primary template chain: stop when clang_getSpecializedCursorTemplate returns null — do not assume CLASS_TEMPLATE is always the terminus.

