import json
import os
from http.server import BaseHTTPRequestHandler
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from vercel_kv import kv

# --- КОНФИГУРАЦИЯ ---
# Ключ, под которым мы будем хранить состояние сессии в Vercel KV
KV_SESSION_KEY = "" 
# Время жизни сессии в секундах (24 часа)
SESSION_TTL_SECONDS = 86400

# Шаблон HTML для выполнения кода Puter.js
PUTER_HTML_TEMPLATE = """
<html>
<head><script src="https://js.puter.com/v2/"></script></head>
<body>
    <script>
        async function checkLogin() { return await puter.auth.isLoggedIn(); }
        async function getAiResponse(prompt) {
            try {
                const response = await puter.ai.chat(prompt);
                return { success: true, content: response.message.content };
            } catch (error) {
                return { success: false, error: error.toString() };
            }
        }
    </script>
</body>
</html>
"""

class handler(BaseHTTPRequestHandler):

    def do_POST(self):
        # 1. --- Начальная проверка и получение данных ---
        print("INFO: Request received. Starting proxy logic.")
        try:
            content_length = int(self.headers['Content-Length'])
            body = json.loads(self.rfile.read(content_length))
            prompt = body.get('prompt')

            puter_email = os.environ.get('PUTER_EMAIL')
            puter_password = os.environ.get('PUTER_PASSWORD')

            if not all([prompt, puter_email, puter_password]):
                self._send_json_response(400, {'error': 'Prompt, PUTER_EMAIL, or PUTER_PASSWORD env var is not set.'})
                return

        except Exception as e:
            print(f"ERROR: Could not parse request body: {e}")
            self._send_json_response(400, {'error': 'Invalid request body.'})
            return

        # 2. --- Основная логика с Playwright ---
        playwright = None
        browser = None
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                print("INFO: Headless browser launched.")

                # 3. --- Управление сессией ---
                print(f"INFO: Attempting to load session from KV with key: {KV_SESSION_KEY}")
                auth_state = kv.get(KV_SESSION_KEY)
                
                if auth_state:
                    print("INFO: Found existing session in KV. Loading it into browser context.")
                    context = browser.new_context(storage_state=json.loads(auth_state))
                else:
                    print("INFO: No session found in KV. Creating a new browser context.")
                    context = browser.new_context()
                
                page = context.new_page()
                page.set_content(PUTER_HTML_TEMPLATE)

                print("INFO: Checking login status...")
                is_logged_in = page.evaluate("checkLogin()")

                if not is_logged_in:
                    if auth_state:
                        print("WARNING: Session from KV is stale or invalid. Performing full login.")
                    else:
                        print("INFO: No active session. Performing full login.")
                    
                    self._perform_full_login(page, puter_email, puter_password)
                    
                    print("INFO: Login successful. Saving new session state to KV.")
                    new_auth_state = context.storage_state()
                    kv.set(KV_SESSION_KEY, json.dumps(new_auth_state), ex=SESSION_TTL_SECONDS)
                    print(f"INFO: Session state saved to KV. TTL: {SESSION_TTL_SECONDS} seconds.")
                else:
                    print("INFO: Session is active and valid. Skipping login.")

                # 4. --- Выполнение AI запроса ---
                print(f"INFO: Executing AI prompt: '{prompt[:30]}...'")
                result = self._get_ai_response(page, prompt)

                if result.get('success'):
                    print("INFO: AI response received successfully.")
                    self._send_json_response(200, {'response': result.get('content')})
                else:
                    raise Exception(f"Browser-side error from Puter.js: {result.get('error')}")

        except PlaywrightTimeoutError as e:
            print(f"FATAL: Playwright timeout error: {e}")
            kv.delete(KV_SESSION_KEY) # Удаляем сессию, т.к. она, вероятно, вызвала проблему
            print("INFO: Corrupted session key deleted from KV due to timeout.")
            self._send_json_response(500, {'error': f"Operation timed out. The server may be slow. Please try again. Details: {e}"})
        except Exception as e:
            print(f"FATAL: An unexpected error occurred: {e}")
            kv.delete(KV_SESSION_KEY)
            print("INFO: Corrupted session key deleted from KV due to error.")
            self._send_json_response(500, {'error': f"An internal server error occurred: {str(e)}"})
        finally:
            # 5. --- Гарантированная очистка ресурсов ---
            if browser:
                browser.close()
                print("INFO: Browser closed.")


    def _perform_full_login(self, page, email, password):
        """Выполняет полную процедуру входа с обработкой popup-окна."""
        print("INFO: Starting popup-based login flow.")
        
        # Ждем события открытия popup-окна, которое произойдет после вызова signIn
        with page.expect_popup(timeout=15000) as popup_info:
            page.evaluate("puter.auth.signIn()")
        
        popup = popup_info.value
        print("INFO: Popup window detected.")
        popup.wait_for_load_state('load', timeout=15000)
        print("INFO: Popup page loaded.")

        # Используем более надежные селекторы get_by_...
        print("INFO: Filling email...")
        popup.get_by_label("Email").fill(email)
        popup.get_by_role("button", name="Continue with Email").click()
        print("INFO: 'Continue with Email' button clicked.")

        # Ждем появления поля пароля
        print("INFO: Waiting for password field...")
        password_input = popup.get_by_label("Password")
        password_input.wait_for(state='visible', timeout=10000)
        
        print("INFO: Filling password...")
        password_input.fill(password)
        popup.get_by_role("button", name="Sign In").click()
        print("INFO: 'Sign In' button clicked.")

        # Ждем, пока popup закроется сам после успешного входа
        print("INFO: Waiting for popup to close...")
        popup.wait_for_event('close', timeout=20000)
        print("INFO: Popup closed, login assumed successful.")


    def _get_ai_response(self, page, prompt):
        """Вызывает JS-функцию для получения ответа от AI."""
        return page.evaluate("getAiResponse(prompt)", prompt)


    def _send_json_response(self, status_code, data):
        """Отправляет унифицированный JSON-ответ."""
        self.send_response(status_code)
        self.send_header('Content-type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))