import api_utils

api = api_utils.API()

@api.GET("/api/")
def default(request):
    return "Willkommen zum Todo-API"

todos = [
    "Abwaschen",
    "Aufräumen",
    "Kette ölen (Velo)"
]  # "Datenbank"

@api.GET("/api/todos/")
def list_todos(request):
    return todos

@api.DELETE("/api/todos/<int:index>")
def delete_entry(request, index):
    del todos[index]

@api.POST("/api/todos/")
def new_entry(request, text:str):
    todos.append(text)

api_utils.run(api, hostname="127.0.0.1", port=5000)