(defun fact (n)
    (if (= n 0)
        1
        (* n (call fact (- n 1)))
    )
)

(print (call fact 6))