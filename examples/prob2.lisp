(defvar sum 0)      
(defvar sum_sq 0)   

;; Основной цикл от 1 до 100 включительно
(loop i 1 101
    (progn
        ;; sum_sq = sum_sq + (i * i)
        (setq sum_sq (+ sum_sq (* i i)))
        ;; sum = sum + i
        (setq sum (+ sum i))
    )
)

;; Квадрат суммы: sum^2
(defvar sq_sum (* sum sum))

;; Разница: (квадрат суммы) - (сумма квадратов)
(defvar result (- sq_sum sum_sq))

;; Вывод результатов
(print-pstr "Result: ")
(print result)