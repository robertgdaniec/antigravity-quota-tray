# PRZEWODNIK STYLU I STANDARDU INŻYNIERSKIEGO (STYLEGUIDE)

Niniejszy dokument definiuje standardy rzemiosła programistycznego i czystego kodu obowiązujące w projekcie **Antigravity QuotaTray**.

---

## 1. Środowisko i Standard Języka Python (Python 3.13+)

1. **Typowanie Statyczne:**
   - Stosujemy nowoczesne unie typów `X | None` zamiast przestarzałego `Optional[X]`.
   - Korzystamy z wbudowanych generyków kolekcji (`list[dict]`, `dict[str, Any]`, `tuple[int, ...]`) zamiast importów z modułu `typing`.
   - Wszystkie funkcje publiczne posiadają precyzyjne adnotacje typów argumentów oraz typu zwracanego.

2. **Formatowanie i Konwencje Nazewnicze:**
   - Zgodność z PEP 8: `snake_case` dla funkcji i zmiennych, `PascalCase` dla klas i struktur ctypes, `SCREAMING_SNAKE_CASE` dla stałych konfiguracyjnych.
   - Moduły posiadają zwięzłe docstringi opisujące ich odpowiedzialność architektoniczną.

---

## 2. Standard Integracji z Systemem Windows (Win32 P/Invoke)

1. **Jawne Typowanie CTypes:**
   - Każde wywołanie funkcji Win32 z bibliotek systemowych (`advapi32.dll`, `user32.dll`, `shell32.dll`) posiada jawnie zdefiniowane typy `argtypes` oraz `restype`.
   - Struktury systemowe dziedziczą po `ctypes.Structure` z jednoznacznymi polami z `ctypes.wintypes`.

2. **Dyscyplina Zarządzania Pamięcią:**
   - Każda alokacja bufora lub odczyt poświadczeń przez `CredReadW` posiada parzyste zwolnienie w bloku `finally` za pomocą `CredFree(pcred)`.
   - Uchwyty okien Win32 (`HWND`) i menu (`HMENU`) podlegają zwalnianiu po zakończeniu procedur obsługi komunikatów.

---

## 3. Integralność Danych i Operacje Wejścia/Wyjścia (I/O)

1. **Wymuszenie Kodowania UTF-8:**
   - Wszelkie operacje plikowe jawnie definiują parametr `encoding="utf-8"`.
   - Strumienie konsoli `sys.stdout` i `sys.stderr` są rekonfigurowane do kodowania UTF-8 na starcie każdego procesu.

2. **Atomowy Zapis Danych Dyskowych:**
   - Zapis rejestru `account_quotas.json` oraz sesji w skarbcu odbywa się wyłącznie z wykorzystaniem wzorca zapisu do pliku tymczasowego z identyfikatorem procesu (`.tmp.<pid>`), a następnie atomowej podmiany przez `os.replace`.

3. **Determinizm Ścieżek:**
   - Wszystkie ścieżki systemowe budowane są w oparciu o `pathlib.Path`.
   - Ścieżki bazowe odnoszą się dynamicznie do katalogu domowego (`Path.home() / ".gemini"`) lub katalogu bieżącego modułu (`Path(__file__).parent`).

---

## 4. Komunikacja i Interfejs Użytkownika (Positive Framing)

1. **Kanon Afirmatywnej Gramatyki (ADR-047):**
   - Etykiety menu, komunikaty konsoli oraz generowane raporty stosują gramatykę twierdzącą czasu teraźniejszego:
     `[Narzędzie / Podmiot] + [Czasownik twierdzący w czasie teraźniejszym] + [Obserwowalny fakt / Korzyść / Gotowy rezultat]`.
   - Prezentacja statusu kont opiera się na faktach: *„Limit Gemini: 98% (Reset za 3h 20m)”*, *„Sesja aktywna i zabezpieczona w skarbcu”*.

2. **Informacja Zwrotna (Visual Freshness):**
   - Każda operacja asynchroniczna lub odświeżenie danych dostarcza natychmiastowego potwierdzenia wizualnego (np. dynamiczny znacznik czasu `(HH:MM:SS)` w menu zasobnika).
