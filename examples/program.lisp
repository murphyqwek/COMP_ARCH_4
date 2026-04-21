; Определяем обработчик прерывания
(def-interrupt 
  (defvar last-key 0)
  (setq last-key (in-char)) ; в трансляторе можно добавить маппинг на 0x2004
)

; Выводим строку (Pascal String внутри)
(print-pstr "Hello ")

; CISC + Variable Args
(defvar sum (+ 1 2 3 4))
(print sum)

; Вызов функции
(defun square (x)
    (setq sum (* x x))
)

; Common Lisp Style loop
(loop i 1 5
    (call square i)
    (print sum)
)