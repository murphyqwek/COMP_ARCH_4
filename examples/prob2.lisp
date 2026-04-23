(defvar sum 0)      
(defvar sum_sq 0)   

(loop i 1 101
    (progn
        (setq sum_sq (+ sum_sq (* i i)))
        (setq sum (+ sum i))))

(defvar sq_sum (* sum sum))

(defvar result (- sq_sum sum_sq))

(print-pstr "Result: ")
(print result)